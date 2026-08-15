"""The async solve path, described rather than scripted.

This is the CDK equivalent of infra/provision_async.py. Same resources: a
queue with a dead-letter queue, a Lambda worker fed by that queue, and a
Step Functions state machine with a durable human-approval gate.

What is different is what ISN'T here. There is no create-versus-update
branching, no retry loop for IAM role propagation, no wait-for-ready polling,
and no hand-written IAM policy for the queue - CDK derives the permission from
the fact that the worker is wired to the queue. It also knows what it created,
so deleting a resource from this file deletes it from the account on the next
deploy. The script could never do that.

Physical names are left to CDK on purpose. Hardcoding them stops you deploying
a second copy (a staging stack) and forces awkward replacements later; the real
names come out as stack outputs instead.
"""
from pathlib import Path

from aws_cdk import (
    CfnOutput,
    Duration,
    Stack,
)
from aws_cdk import (
    RemovalPolicy,
)
from aws_cdk import (
    aws_codebuild as codebuild,
)
from aws_cdk import (
    aws_cognito as cognito,
)
from aws_cdk import (
    aws_dynamodb as dynamodb,
)
from aws_cdk import (
    aws_ecr as ecr,
)
from aws_cdk import (
    aws_iam as iam,
)
from aws_cdk import (
    aws_lambda as lambda_,
)
from aws_cdk import (
    aws_sqs as sqs,
)
from aws_cdk import (
    aws_stepfunctions as sfn,
)
from aws_cdk import (
    aws_stepfunctions_tasks as tasks,
)
from aws_cdk.aws_lambda_event_sources import SqsEventSource
from constructs import Construct

WORKER_DIR = str(Path(__file__).resolve().parent / "worker")


class AsyncSolveStack(Stack):
    def __init__(self, scope: Construct, construct_id: str, *,
                 result_bucket: str, worker_image_tag: str | None = None,
                 **kwargs) -> None:
        """`worker_image_tag` selects the container worker.

        Left as None the stack deploys the zip worker, which is what makes the
        very first deploy possible: a Lambda cannot reference an ECR image that
        does not exist yet, and the image is built by the CodeBuild project
        this stack creates. So the order is deploy (zip) -> build image ->
        deploy again with the tag. Encoding that as a parameter beats a deploy
        that fails until someone reads the runbook.
        """
        super().__init__(scope, construct_id, **kwargs)

        # The container worker needs longer than the zip one: it may pull a
        # model directory from S3 and unpickle scikit-learn objects.
        worker_timeout = Duration.minutes(5) if worker_image_tag else Duration.minutes(2)

        # -- queue + dead letter ------------------------------------------
        dead_letters = sqs.Queue(
            self, "SolvesDlq",
            retention_period=Duration.days(14),
        )
        queue = sqs.Queue(
            self, "Solves",
            # SQS rejects an event source mapping whose visibility timeout is
            # below the function timeout, because it would redeliver a message
            # still being processed. Derived rather than hardcoded: stating the
            # rule in a comment is what let a later timeout bump break it.
            visibility_timeout=Duration.seconds(worker_timeout.to_seconds() + 60),
            retention_period=Duration.days(1),
            dead_letter_queue=sqs.DeadLetterQueue(
                max_receive_count=3, queue=dead_letters,
            ),
        )

        # -- application state --------------------------------------------
        # Phase 5: the module-level dicts that pin the API to one node. Single
        # table, partition per entity type and sort by id, so "get one" and
        # "list all of a type" are both a single call and neither needs a scan.
        #
        # On-demand billing rather than provisioned: request volume here is an
        # analyst clicking, which is exactly the spiky, low-average pattern
        # on-demand exists for, and it stays inside the 25 GB always-free tier.
        table = dynamodb.TableV2(
            self, "State",
            partition_key=dynamodb.Attribute(name="pk",
                                             type=dynamodb.AttributeType.STRING),
            sort_key=dynamodb.Attribute(name="sk",
                                        type=dynamodb.AttributeType.STRING),
            billing=dynamodb.Billing.on_demand(),
            # RETAIN, not DESTROY. This table now holds the analyst's datasets,
            # models, saved workflows and run history - the evidence trail for
            # numbers that get filed. A `cdk destroy`, or any change
            # CloudFormation decides is a replacement, would take all of it.
            #
            # The cost of RETAIN is an orphaned table if the stack is torn down,
            # which has to be deleted by hand. That is the correct direction to
            # fail: recovering from an unwanted leftover is tedious, recovering
            # from a deleted audit trail is impossible.
            removal_policy=RemovalPolicy.RETAIN,
            # Point-in-time recovery gives 35 days of second-granularity restore.
            # It bills on stored bytes, which at this volume is cents, and it is
            # the only defence against a bad write - versioning protects the
            # documents in S3, but nothing protected these records.
            point_in_time_recovery_specification=(
                dynamodb.PointInTimeRecoverySpecification(
                    point_in_time_recovery_enabled=True)
            ),
        )

        # Runs are the one entity that grows without bound - one item per
        # execution, forever - and the run-history page wants the newest N for
        # one function. On the base table that question can only be answered by
        # reading every run ever and sorting in Python, because the sort key is
        # a random id.
        #
        # This index re-keys the same items by (function, time), so "newest 50"
        # becomes a range read that stops after 50 items. The cost of a page
        # stops depending on how many runs exist.
        #
        # Sparse by construction: only items written with gsi1pk/gsi1sk appear,
        # so datasets, models and workflows are simply absent from it and cost
        # nothing. Named generically rather than "by-run" because the same two
        # attributes are the natural home for the next time-ordered collection.
        #
        # Projection is ALL, which keeps `list_runs` returning whole runs as it
        # does today. Switching to INCLUDE without `series` would shrink the
        # index enormously, but it would also empty the series out of list
        # responses - a frontend-visible change, so it is a separate decision.
        table.add_global_secondary_index(
            index_name="gsi1",
            partition_key=dynamodb.Attribute(name="gsi1pk",
                                             type=dynamodb.AttributeType.STRING),
            sort_key=dynamodb.Attribute(name="gsi1sk",
                                        type=dynamodb.AttributeType.STRING),
            projection_type=dynamodb.ProjectionType.ALL,
        )

        # -- identity -----------------------------------------------------
        # The last piece of per-process state. `routers/auth.py` mints a UUID
        # and remembers what it stands for in a module dict, so a second
        # replica rejects a token the first one issued. A Cognito JWT carries
        # its claims instead, and every replica verifies it offline against
        # this pool's public keys - nothing is looked up, so nothing is shared.
        #
        # `custom:role` and `custom:department` exist because the workbench
        # already shows both on the user badge, and `cognito:groups` maps onto
        # the `groups` list that `is_pack_visible` filters domain packs by. The
        # claim names are the contract with services/cognito_auth.py.
        user_pool = cognito.UserPool(
            self, "Users",
            self_sign_up_enabled=False,          # analysts are provisioned, not registered
            sign_in_aliases=cognito.SignInAliases(username=True, email=True),
            standard_attributes=cognito.StandardAttributes(
                email=cognito.StandardAttribute(required=True, mutable=True),
            ),
            custom_attributes={
                "role": cognito.StringAttribute(max_len=128, mutable=True),
                "department": cognito.StringAttribute(max_len=128, mutable=True),
            },
            password_policy=cognito.PasswordPolicy(
                min_length=12,
                require_lowercase=True, require_uppercase=True,
                require_digits=True, require_symbols=False,
            ),
            # A lab pool. Real deployments keep this and turn on MFA.
            removal_policy=RemovalPolicy.DESTROY,
        )

        user_pool_client = user_pool.add_client(
            "WorkbenchClient",
            # No secret: the token is verified by signature, not by the caller
            # proving it holds one, and a secret would mean every InitiateAuth
            # call also computes a SECRET_HASH for no security gain here.
            generate_secret=False,
            auth_flows=cognito.AuthFlow(
                # Keeps the existing login form working: the frontend still
                # posts a username and password and gets a token back. The
                # production path is the Hosted UI authorization-code flow,
                # which keeps the password out of this service entirely.
                user_password=True,
                user_srp=True,
            ),
            # Short, because offline verification cannot see a sign-out: a
            # revoked session's ID token stays valid until it expires, so the
            # TTL *is* the revocation window.
            id_token_validity=Duration.hours(1),
            access_token_validity=Duration.hours(1),
            refresh_token_validity=Duration.days(30),
            prevent_user_existence_errors=True,
        )

        # -- image registry + remote builder ------------------------------
        # The image is built in AWS rather than locally: no Docker on the
        # developer's machine, and the build is reproducible in CI.
        image_repo = ecr.Repository(
            self, "WorkerImage",
            image_scan_on_push=True,
            removal_policy=RemovalPolicy.DESTROY,
            empty_on_delete=True,
            lifecycle_rules=[
                # A scikit-learn image is ~277 MB and ECR's free allowance is
                # 500 MB, so TWO tags already exceed it. Keeping one is what
                # holds this account at zero cost.
                #
                # The trade-off, stated plainly: there is no previous image to
                # roll back to. Rebuilding from a known-good source tree is the
                # recovery path instead, which is acceptable because the tag is
                # a hash of that tree — but it is slower than repointing at an
                # existing tag. Raise this to 2 and accept ~5c/month if you
                # would rather have instant rollback.
                ecr.LifecycleRule(max_image_count=1),
            ],
        )

        builder = codebuild.Project(
            self, "WorkerImageBuild",
            project_name=f"{construct_id}-worker-image",
            # Source arrives as a zip in the results bucket, uploaded by
            # infra/build_worker_image.py. Avoids wiring a git provider for
            # what is a handful of files.
            source=codebuild.Source.s3(
                bucket=__import__("aws_cdk").aws_s3.Bucket.from_bucket_name(
                    self, "SourceBucket", result_bucket),
                path="builds/worker-image-src.zip",
            ),
            environment=codebuild.BuildEnvironment(
                build_image=codebuild.LinuxBuildImage.STANDARD_7_0,
                # Docker needs a privileged build container.
                privileged=True,
                compute_type=codebuild.ComputeType.SMALL,
            ),
            environment_variables={
                "IMAGE_REPO": codebuild.BuildEnvironmentVariable(
                    value=image_repo.repository_uri),
                "AWS_ACCOUNT_ID": codebuild.BuildEnvironmentVariable(
                    value=self.account),
                # Overridden per build by infra/build_worker_image.py with a
                # hash of the source; the default only exists so a build
                # started by hand from the console still works.
                "IMAGE_TAG": codebuild.BuildEnvironmentVariable(value="manual"),
            },
            timeout=Duration.minutes(30),
            build_spec=codebuild.BuildSpec.from_object({
                "version": "0.2",
                # Everything in one phase on purpose: CodeBuild runs each phase
                # as a separate shell, so a variable set in pre_build is gone by
                # build. IMAGE_TAG is supplied by the caller rather than derived
                # here, so the tag is known before the build starts and is a
                # deterministic function of the source.
                "phases": {
                    "build": {"commands": [
                        'echo "building ${IMAGE_TAG}"',
                        "aws ecr get-login-password --region $AWS_DEFAULT_REGION "
                        "| docker login --username AWS --password-stdin "
                        "$AWS_ACCOUNT_ID.dkr.ecr.$AWS_DEFAULT_REGION.amazonaws.com",
                        "docker build -t $IMAGE_REPO:$IMAGE_TAG "
                        "-t $IMAGE_REPO:latest .",
                        "docker push $IMAGE_REPO:$IMAGE_TAG",
                        "docker push $IMAGE_REPO:latest",
                    ]},
                },
            }),
        )
        image_repo.grant_pull_push(builder)

        # -- worker -------------------------------------------------------
        common_env = {"RESULT_BUCKET": result_bucket, "RESULT_PREFIX": "jobs/",
                      "MODEL_PREFIX": "models/"}
        if worker_image_tag:
            worker = lambda_.DockerImageFunction(
                self, "SolverImage",
                code=lambda_.DockerImageCode.from_ecr(
                    image_repo, tag_or_digest=worker_image_tag),
                timeout=worker_timeout,
                # A container cold start pulls the image, and unpickling
                # scikit-learn wants headroom; 512 MB is too tight here even
                # though it was fine for the zip worker.
                memory_size=2048,
                environment=common_env,
                description="CMA Workbench solver (container: real model artifacts)",
            )
        else:
            worker = lambda_.Function(
                self, "Solver",
                runtime=lambda_.Runtime.PYTHON_3_12,
                handler="handler.handler",
                code=lambda_.Code.from_asset(WORKER_DIR),
                timeout=worker_timeout,
                memory_size=512,
                environment=common_env,
                description="CMA Workbench async solver (zip: goal-seek only)",
            )

        # One line replaces the event-source mapping AND the SQS half of the
        # worker's IAM policy - CDK infers the permission from the wiring.
        worker.add_event_source(SqsEventSource(queue, batch_size=1))

        # Still explicit, because the intent is narrower than any helper:
        # the worker writes results and reads nothing else. Granting the whole
        # bucket would hand it the document corpus for no reason.
        worker.add_to_role_policy(iam.PolicyStatement(
            actions=["s3:PutObject", "s3:GetObject"],
            resources=[f"arn:aws:s3:::{result_bucket}/jobs/*"],
        ))
        # Model artifacts are read-only to the worker, and only under models/.
        worker.add_to_role_policy(iam.PolicyStatement(
            actions=["s3:GetObject"],
            resources=[f"arn:aws:s3:::{result_bucket}/models/*"],
        ))
        worker.add_to_role_policy(iam.PolicyStatement(
            actions=["s3:ListBucket"],
            resources=[f"arn:aws:s3:::{result_bucket}"],
            conditions={"StringLike": {"s3:prefix": ["models/*", "jobs/*"]}},
        ))

        # -- playbook state machine ---------------------------------------
        solve = tasks.LambdaInvoke(
            self, "Solve",
            lambda_function=worker,
            payload=sfn.TaskInput.from_object({
                "mode": "solve",
                "job_id": sfn.JsonPath.string_at("$.run_id"),
                "params": sfn.JsonPath.object_at("$.params"),
            }),
            payload_response_only=True,
            result_path="$.solve",
        )
        solve.add_retry(
            errors=["Lambda.ServiceException", "Lambda.TooManyRequestsException",
                    "States.TaskFailed"],
            interval=Duration.seconds(2), max_attempts=3, backoff_rate=2.0,
        )

        failed = sfn.Fail(self, "Failed", error="SolveFailed",
                          cause="The solve step failed after retries")
        solve.add_catch(failed, errors=["States.ALL"], result_path="$.error")

        # WAIT_FOR_TASK_TOKEN is what makes the pause durable: the execution
        # holds here until SendTaskSuccess arrives with the reviewer's answer.
        approval = tasks.LambdaInvoke(
            self, "ApprovalGate",
            lambda_function=worker,
            integration_pattern=sfn.IntegrationPattern.WAIT_FOR_TASK_TOKEN,
            payload=sfn.TaskInput.from_object({
                "mode": "request_approval",
                "run_id": sfn.JsonPath.string_at("$.run_id"),
                "phase": "champion_challenger",
                "task_token": sfn.JsonPath.task_token,
            }),
            result_path="$.approval",
            # task_timeout, not timeout - the latter is deprecated in CDK and
            # synth says so, which is a nice property of the tool: the
            # hand-written boto3 version had no way to tell me.
            task_timeout=sfn.Timeout.duration(Duration.hours(24)),
        )
        expired = sfn.Fail(self, "ApprovalExpired", error="ApprovalTimeout",
                           cause="No reviewer approved within 24 hours")
        approval.add_catch(expired, errors=["States.Timeout"], result_path="$.error")

        publish = tasks.LambdaInvoke(
            self, "Publish",
            lambda_function=worker,
            payload=sfn.TaskInput.from_object({
                "mode": "publish",
                "run_id": sfn.JsonPath.string_at("$.run_id"),
                "approved_by": sfn.JsonPath.string_at("$.approval.approved_by"),
            }),
            payload_response_only=True,
            result_path="$.publish",
        )

        machine = sfn.StateMachine(
            self, "Playbook",
            definition_body=sfn.DefinitionBody.from_chainable(
                solve.next(approval).next(publish)
            ),
            timeout=Duration.days(2),
            comment="CMA Workbench playbook: solve, human approval, publish",
        )

        # -- outputs ------------------------------------------------------
        # These are how backend/.env gets its values; nothing is hardcoded.
        CfnOutput(self, "QueueUrl", value=queue.queue_url,
                  description="CMA_JOBS_QUEUE_URL")
        CfnOutput(self, "StateMachineArn", value=machine.state_machine_arn,
                  description="CMA_PLAYBOOK_STATE_MACHINE")
        CfnOutput(self, "WorkerFunctionName", value=worker.function_name,
                  description="Lambda worker")
        CfnOutput(self, "DeadLetterQueueUrl", value=dead_letters.queue_url,
                  description="Failed solves land here after 3 attempts")
        # Needed by services/workflow_deploy.py, which compiles Workflow-tab
        # canvases into their own state machines: the Task states invoke this
        # function, and Step Functions assumes this role to do it.
        CfnOutput(self, "SolverFunctionArn", value=worker.function_arn,
                  description="CMA_SOLVER_FUNCTION_ARN")
        CfnOutput(self, "PlaybookRoleArn", value=machine.role.role_arn,
                  description="CMA_PLAYBOOK_ROLE_ARN")
        CfnOutput(self, "StateTableName", value=table.table_name,
                  description="CMA_STATE_TABLE")
        CfnOutput(self, "UserPoolId", value=user_pool.user_pool_id,
                  description="CMA_COGNITO_USER_POOL_ID")
        CfnOutput(self, "UserPoolClientId", value=user_pool_client.user_pool_client_id,
                  description="CMA_COGNITO_CLIENT_ID")
        CfnOutput(self, "WorkerImageRepo", value=image_repo.repository_uri,
                  description="ECR repo for the container worker")
        CfnOutput(self, "WorkerImageBuildProject", value=builder.project_name,
                  description="CodeBuild project that builds the worker image")
        CfnOutput(self, "WorkerFlavour",
                  value=f"container:{worker_image_tag}" if worker_image_tag else "zip",
                  description="Which worker this deploy wired up")
