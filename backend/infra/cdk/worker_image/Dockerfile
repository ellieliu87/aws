# Container image for the solve worker.
#
# Why a container and not a zip: 6 of the 8 bundled model artifacts unpickle
# with only a local `_classes` module and numpy, but mortgage_rate_ts.pkl and
# prepayment_nn.pkl carry scikit-learn objects. scikit-learn plus scipy, numpy
# and pandas does not reliably fit Lambda's 250 MB unzipped limit, so the
# artifacts themselves decided this — not a preference for containers.
#
# Built by CodeBuild, not locally. See infra/build_worker_image.py.
FROM public.ecr.aws/lambda/python:3.12

# Versions are pinned to the environment that CREATED the pickles. Unpickling
# a scikit-learn estimator across a major version boundary either warns or
# fails outright, and a silent numeric difference would be worse than either.
# Keep these in step with backend/pyproject.toml.
RUN pip install --no-cache-dir \
      numpy==2.4.4 \
      pandas==3.0.2 \
      scikit-learn==1.8.0 \
      joblib==1.5.3

COPY handler.py ${LAMBDA_TASK_ROOT}/

CMD ["handler.handler"]
