import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import {
  ReactFlow, ReactFlowProvider, Background, Controls, MiniMap,
  Handle, Position, MarkerType, ConnectionMode, useReactFlow,
  addEdge, applyEdgeChanges, applyNodeChanges,
  type Connection, type Edge, type EdgeChange,
  type Node as RFNode, type NodeChange, type NodeProps,
} from '@xyflow/react'
import '@xyflow/react/dist/style.css'

import {
  FlaskConical, Play, X, Sparkles, Trash2, ChevronRight, AlertCircle,
  CheckCircle2, Database, Boxes, GripVertical, Hand, Layers,
  Snowflake, Cloud, HardDrive, FileText, Settings as SettingsIcon, Download,
  Loader2, Workflow as WorkflowIcon, Save, Bookmark,
} from 'lucide-react'
import api from '@/lib/api'
import { useChatStore } from '@/store/chatStore'
import type {
  AnalyticsRun, Scenario, TrainedModel, Dataset, WorkflowResult,
  DestinationKind, DestinationWrite, NodeRunStatus, WorkflowValidationResult,
  WorkflowValidationIssue, WorkflowRunErrorDetail, Transform, DataSource,
  SavedWorkflow, SavedWorkflowSummary,
} from '@/types'
import {
  ResponsiveContainer, LineChart, Line, CartesianGrid, XAxis, YAxis,
  Tooltip as RTooltip, Legend,
} from 'recharts'
import WorkflowStepsView from './WorkflowStepsView'
import WorkflowSpecView from './WorkflowSpecView'
import { ListOrdered, FileCode } from 'lucide-react'

type WorkflowView = 'steps' | 'canvas' | 'spec'

const SEVERITY_COLOR: Record<Scenario['severity'], string> = {
  base: '#059669',
  outlook: '#0891B2',
  adverse: '#D97706',
  severely_adverse: '#DC2626',
  custom: '#7C3AED',
}

const SEVERITY_LABEL: Record<Scenario['severity'], string> = {
  base: 'BASE',
  outlook: 'OUTLOOK',
  adverse: 'ADVERSE',
  severely_adverse: 'SEVERE',
  custom: 'CUSTOM',
}

interface Props {
  functionId: string
  functionName: string
  onAskAgent: (q: string) => void
  onContextChange: (ctx: string | null) => void
}

interface NodeData extends Record<string, unknown> {
  kind: 'dataset' | 'scenario' | 'model' | 'destination' | 'transform'
  ref_id: string
  title: string
  subtitle: string
  color: string
  config?: Record<string, any>
  status?: NodeRunStatus
}

const PALETTE_DRAG_MIME = 'application/x-cma-palette'

// Destination type metadata — each is a "blank" palette card; user fills config on drop
const DESTINATION_META: Record<DestinationKind, { label: string; icon: any; color: string; placeholder: string; configHint: string }> = {
  snowflake_table: {
    label: 'Snowflake Table', icon: Snowflake, color: '#29B5E8',
    placeholder: 'CMA.PUBLIC.OUTPUT',
    configHint: 'Fully-qualified table name (DATABASE.SCHEMA.TABLE)',
  },
  onelake_table: {
    label: 'OneLake Table', icon: Cloud, color: '#0078D4',
    placeholder: 'Finance.cma.output',
    configHint: 'Lakehouse table reference (workspace.lakehouse.table)',
  },
  s3: {
    label: 'S3 Bucket', icon: HardDrive, color: '#D97706',
    placeholder: 'cma-outputs',
    configHint: 'Bucket name (key prefix is set automatically)',
  },
  csv: {
    label: 'CSV File', icon: FileText, color: '#7C3AED',
    placeholder: 'output.csv',
    configHint: 'Filename — file is downloaded to your machine',
  },
}

const STATUS_COLOR: Record<NodeRunStatus, string> = {
  idle: 'transparent',
  running: '#0891B2',
  completed: '#059669',
  failed: '#DC2626',
  skipped: '#A1A1AA',
}

export default function AnalyticsTab(props: Props) {
  return (
    <ReactFlowProvider>
      <AnalyticsCanvas {...props} />
    </ReactFlowProvider>
  )
}

function AnalyticsCanvas({ functionId, functionName, onAskAgent, onContextChange }: Props) {
  const setEntity = useChatStore((s) => s.setEntity)
  const setPayload = useChatStore((s) => s.setPayload)
  const setOpen = useChatStore((s) => s.setOpen)
  const [scenarios, setScenarios] = useState<Scenario[]>([])
  const [models, setModels] = useState<TrainedModel[]>([])
  const [datasets, setDatasets] = useState<Dataset[]>([])
  const [transforms, setTransforms] = useState<Transform[]>([])
  const [dataSources, setDataSources] = useState<DataSource[]>([])
  const [runs, setRuns] = useState<AnalyticsRun[]>([])
  const [activeRun, setActiveRun] = useState<AnalyticsRun | null>(null)
  const [transformPanelFor, setTransformPanelFor] = useState<string | null>(null)

  // Workflow state — initialized lazily from localStorage so a tab
  // switch (which unmounts AnalyticsTab) or a page reload doesn't lose
  // the user's in-progress canvas. Keyed by function_id.
  const STORAGE_KEY = `cma:workflow:draft:${functionId}`
  const draft = (() => {
    try {
      const raw = localStorage.getItem(STORAGE_KEY)
      return raw ? JSON.parse(raw) : null
    } catch { return null }
  })()
  const [nodes, setNodes] = useState<RFNode<NodeData>[]>(() => draft?.nodes || [])
  const [edges, setEdges] = useState<Edge[]>(() => draft?.edges || [])
  // CCAR submission horizon is 9 quarters = 27 months. Default to that so
  // a fresh workflow lines up with the regulatory ask without the analyst
  // having to remember the number every time. They can still override.
  const [horizon, setHorizon] = useState<number>(() =>
    typeof draft?.horizon === 'number' ? draft.horizon : 27
  )
  const [running, setRunning] = useState(false)
  const [error, setError] = useState<string | null>(null)
  // Run-context inputs — pinned alongside Horizon. Scenario dropdown
  // sources from the same /api/analytics/scenarios fetch the canvas
  // palette uses, so it stays in sync with Data Services.
  const [scenarioName, setScenarioName] = useState<string>(() => draft?.scenarioName || '')
  const [startDate, setStartDate] = useState<string>(() => draft?.startDate || '')
  const [validation, setValidation] = useState<WorkflowValidationResult | null>(null)
  const [validating, setValidating] = useState(false)
  const [runError, setRunError] = useState<WorkflowRunErrorDetail | null>(null)
  const [latestWorkflowId, setLatestWorkflowId] = useState<string | null>(null)
  const [lastResult, setLastResult] = useState<WorkflowResult | null>(null)
  const [destConfigFor, setDestConfigFor] = useState<{ nodeId: string; kind: DestinationKind } | null>(null)
  const [view, setView] = useState<WorkflowView>(() => draft?.view || 'steps')

  // Saved-workflow registry (server-side) + the currently loaded record
  // (so Save can update-in-place rather than always create a new one).
  const [savedWorkflows, setSavedWorkflows] = useState<SavedWorkflowSummary[]>([])
  const [activeSavedId, setActiveSavedId] = useState<string | null>(() => draft?.activeSavedId || null)
  const [activeSavedName, setActiveSavedName] = useState<string | null>(() => draft?.activeSavedName || null)
  const [savedMenuOpen, setSavedMenuOpen] = useState(false)

  const flowRef = useRef<HTMLDivElement>(null)
  const { screenToFlowPosition } = useReactFlow()

  const load = () => {
    Promise.all([
      api.get<Scenario[]>(`/api/analytics/scenarios`, { params: { function_id: functionId } }),
      api.get<TrainedModel[]>(`/api/models`, { params: { function_id: functionId } }),
      api.get<Dataset[]>(`/api/datasets`, { params: { function_id: functionId } }),
      api.get<AnalyticsRun[]>(`/api/analytics/runs`, { params: { function_id: functionId } }),
      api.get<Transform[]>(`/api/transforms`, { params: { function_id: functionId } }),
      api.get<DataSource[]>(`/api/datasources`),
    ]).then(([s, m, d, r, t, ds]) => {
      setScenarios(s.data); setModels(m.data); setDatasets(d.data); setRuns(r.data)
      setTransforms(t.data); setDataSources(ds.data)
    })
  }
  useEffect(load, [functionId])

  // Saved-workflow list — refreshed independently so Save / Delete
  // updates the dropdown without re-fetching everything.
  const loadSavedWorkflows = useCallback(() => {
    api.get<SavedWorkflowSummary[]>('/api/analytics/workflows', {
      params: { function_id: functionId },
    })
      .then((r) => setSavedWorkflows(r.data))
      .catch(() => setSavedWorkflows([]))
  }, [functionId])
  useEffect(loadSavedWorkflows, [loadSavedWorkflows])

  // When the analyst switches workspaces (functionId changes), reload
  // canvas state from the new function's draft — otherwise the previous
  // workspace's nodes/edges stay in React state because the useState
  // initializers above only run on first mount. A fresh workspace gets an
  // empty canvas; a workspace with a saved draft gets its draft back.
  //
  // `initializedForFn` tracks the functionId the state in scope was last
  // loaded for. On functionId change we reload AND tell the persist
  // effect below to skip its next run — otherwise it'd write the
  // previous workspace's nodes/edges (still in scope this commit) to the
  // new workspace's localStorage key.
  const initializedForFn = useRef(functionId)
  const skipNextPersist = useRef(false)
  useEffect(() => {
    if (initializedForFn.current === functionId) return
    initializedForFn.current = functionId
    skipNextPersist.current = true
    let nextDraft: any = null
    try {
      const raw = localStorage.getItem(STORAGE_KEY)
      nextDraft = raw ? JSON.parse(raw) : null
    } catch { nextDraft = null }
    setNodes(nextDraft?.nodes || [])
    setEdges(nextDraft?.edges || [])
    setHorizon(typeof nextDraft?.horizon === 'number' ? nextDraft.horizon : 27)
    setScenarioName(nextDraft?.scenarioName || '')
    setStartDate(nextDraft?.startDate || '')
    setView(nextDraft?.view || 'steps')
    setActiveSavedId(nextDraft?.activeSavedId || null)
    setActiveSavedName(nextDraft?.activeSavedName || null)
    setValidation(null)
    setRunError(null)
    setLastResult(null)
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [functionId, STORAGE_KEY])

  // Persist the in-progress canvas to localStorage on every change so
  // tab switches + page reloads keep what the user built. The skip flag
  // suppresses exactly one tick after a workspace switch — without it,
  // the previous workspace's state (still in scope this commit) would
  // be copied into the new workspace's storage key, leaving the user
  // with a "stale workflow" on the new workspace.
  useEffect(() => {
    if (skipNextPersist.current) {
      skipNextPersist.current = false
      return
    }
    try {
      localStorage.setItem(STORAGE_KEY, JSON.stringify({
        nodes, edges, horizon, scenarioName, startDate, view,
        activeSavedId, activeSavedName,
      }))
    } catch {}
  }, [STORAGE_KEY, nodes, edges, horizon, scenarioName, startDate, view, activeSavedId, activeSavedName])

  useEffect(() => {
    onContextChange(`${functionName} (Analytics): workflow with ${nodes.length} nodes, ${edges.length} edges, ${runs.length} runs`)
    return () => onContextChange(null)
  }, [nodes.length, edges.length, runs.length, functionName, onContextChange])

  // ── Node / Edge change handlers ────────────────────────────────
  const onNodesChange = useCallback(
    (changes: NodeChange[]) => setNodes((n) => applyNodeChanges(changes, n) as RFNode<NodeData>[]),
    [],
  )
  const onEdgesChange = useCallback(
    (changes: EdgeChange[]) => setEdges((e) => applyEdgeChanges(changes, e)),
    [],
  )

  // Connection rules: only model and destination nodes accept inputs
  const isValidConnection = useCallback((c: Connection | Edge) => {
    if (!c.source || !c.target) return false
    if (c.source === c.target) return false
    const target = nodes.find((n) => n.id === c.target)
    if (!target) return false
    // Transforms can receive input (e.g. Data Harness → DQC) so they're
    // valid edge targets alongside models and destinations.
    return (
      target.data.kind === 'model'
      || target.data.kind === 'destination'
      || target.data.kind === 'transform'
    )
  }, [nodes])

  const onConnect = useCallback(
    (c: Connection) => {
      setEdges((eds) => addEdge({
        ...c,
        animated: true,
        markerEnd: { type: MarkerType.ArrowClosed, color: 'var(--accent)' },
        style: { stroke: 'var(--accent)', strokeWidth: 1.5 },
      }, eds))
    },
    [],
  )

  // ── Drag from palette to canvas ───────────────────────────────
  const onDragOver = useCallback((event: React.DragEvent) => {
    event.preventDefault()
    event.dataTransfer.dropEffect = 'copy'
  }, [])

  const addNode = useCallback((data: NodeData, position: { x: number; y: number }) => {
    const id = `${data.kind}-${data.ref_id}-${Math.random().toString(36).slice(2, 7)}`
    setNodes((n) => [
      ...n,
      {
        id,
        type: data.kind,
        position,
        data: { ...data, status: 'idle' as NodeRunStatus },
      },
    ])
    // For destinations, immediately prompt for the target config
    if (data.kind === 'destination') {
      setDestConfigFor({ nodeId: id, kind: data.ref_id as DestinationKind })
    }
  }, [])

  const updateNodeConfig = useCallback((nodeId: string, config: Record<string, any>, subtitle: string) => {
    setNodes((ns) => ns.map((n) =>
      n.id === nodeId ? { ...n, data: { ...n.data, config, subtitle } } : n
    ))
  }, [])

  // Apply node statuses + clear "running" flag from any node when result arrives
  const applyResultToCanvas = useCallback((result: WorkflowResult) => {
    setNodes((ns) => ns.map((n) => ({
      ...n,
      data: { ...n.data, status: result.node_status[n.id] || 'idle' },
    })))
  }, [])

  const onDrop = useCallback((event: React.DragEvent) => {
    event.preventDefault()
    const raw = event.dataTransfer.getData(PALETTE_DRAG_MIME)
    if (!raw) return
    let data: NodeData
    try { data = JSON.parse(raw) } catch { return }
    const position = screenToFlowPosition({
      x: event.clientX,
      y: event.clientY,
    })
    addNode(data, position)
  }, [screenToFlowPosition, addNode])

  // Click-to-add fallback (drops near top-left of viewport)
  const addFromClick = (data: NodeData) => {
    const bounds = flowRef.current?.getBoundingClientRect()
    if (!bounds) return
    const position = screenToFlowPosition({
      x: bounds.left + 200 + Math.random() * 60,
      y: bounds.top + 80 + Math.random() * 60,
    })
    addNode(data, position)
  }

  // ── Run workflow ──────────────────────────────────────────────
  const submit = async () => {
    if (nodes.length === 0) {
      setError('Drag at least one model and one input onto the canvas.')
      return
    }
    const modelNodes = nodes.filter((n) => n.data.kind === 'model')
    if (modelNodes.length === 0) {
      setError('Add at least one model node.')
      return
    }
    // Validate destination configs upfront
    const unconfigured = nodes.find((n) => n.data.kind === 'destination' && !nodeHasConfig(n.data))
    if (unconfigured) {
      setError(`Destination "${unconfigured.data.title}" needs a target. Click it to configure.`)
      return
    }

    setRunning(true)
    setError(null)
    setRunError(null)
    setLastResult(null)
    // Mark all model + destination nodes as 'running' for immediate visual feedback
    setNodes((ns) => ns.map((n) =>
      n.data.kind === 'model' || n.data.kind === 'destination'
        ? { ...n, data: { ...n.data, status: 'running' as NodeRunStatus } }
        : { ...n, data: { ...n.data, status: 'completed' as NodeRunStatus } }
    ))

    try {
      const r = await api.post<WorkflowResult>('/api/analytics/workflow-runs', {
        function_id: functionId,
        nodes: nodes.map((n) => ({
          id: n.id, kind: n.data.kind, ref_id: n.data.ref_id, config: n.data.config || {},
        })),
        edges: edges.map((e) => ({ source: e.source, target: e.target })),
        horizon_months: horizon,
        scenario_name: scenarioName || undefined,
        start_date: startDate || undefined,
      })
      setLatestWorkflowId(r.data.workflow_id)
      setLastResult(r.data)
      applyResultToCanvas(r.data)
      load()

      // Trigger CSV downloads for any csv destinations
      for (const d of r.data.destinations) {
        if (d.kind === 'csv' && d.csv_data && d.csv_filename) {
          downloadCsv(d.csv_filename, d.csv_data)
        }
      }
      if (r.data.error_detail) {
        setRunError(r.data.error_detail)
      } else if (r.data.error) {
        setError(r.data.error)
      }
    } catch (e: any) {
      // Backend HTTPException(detail=…) — sometimes a structured dict, sometimes a string.
      const detail = e?.response?.data?.detail
      if (detail && typeof detail === 'object') {
        setRunError({
          code: detail.code || 'GENERIC',
          what_happened: detail.error || detail.message || 'Workflow run failed.',
          how_to_fix: detail.hint
            || 'Inspect the failed step\'s details below, or click "Validate" before running again.',
          raw: detail,
        })
      } else {
        setError(typeof detail === 'string' ? detail : 'Workflow run failed')
      }
      setNodes((ns) => ns.map((n) => ({ ...n, data: { ...n.data, status: 'failed' as NodeRunStatus } })))
    } finally {
      setRunning(false)
    }
  }

  const clearCanvas = () => {
    setNodes([])
    setEdges([])
    setError(null)
    setValidation(null)
    setRunError(null)
    // Detach from any loaded saved-workflow so the next Save creates
    // a new record rather than overwriting the one the analyst loaded.
    setActiveSavedId(null)
    setActiveSavedName(null)
    setLastResult(null)
    try { localStorage.removeItem(STORAGE_KEY) } catch {}
  }

  // ── Saved workflows: save / load / delete ────────────────────
  const buildSavePayload = (name: string, description?: string) => ({
    function_id: functionId,
    name,
    description,
    // Persist nodes verbatim — ReactFlow's positions, our NodeData,
    // and any per-node config. The backend round-trips them as opaque
    // dicts; only the frontend interprets the shape.
    nodes,
    edges,
    horizon_months: horizon,
    scenario_name: scenarioName || undefined,
    start_date: startDate || undefined,
    view,
  })

  const saveCurrentWorkflow = async () => {
    if (nodes.length === 0) {
      setError('Nothing to save — drag nodes onto the canvas first.')
      return
    }
    const defaultName = activeSavedName || `Workflow ${new Date().toLocaleString()}`
    const name = window.prompt('Save current workflow as:', defaultName)?.trim()
    if (!name) return
    try {
      if (activeSavedId) {
        // PUT — overwrite the loaded record.
        const r = await api.put<SavedWorkflow>(
          `/api/analytics/workflows/${activeSavedId}`,
          buildSavePayload(name),
        )
        setActiveSavedId(r.data.id)
        setActiveSavedName(r.data.name)
      } else {
        const r = await api.post<SavedWorkflow>('/api/analytics/workflows', buildSavePayload(name))
        setActiveSavedId(r.data.id)
        setActiveSavedName(r.data.name)
      }
      loadSavedWorkflows()
    } catch (e: any) {
      setError(e?.response?.data?.detail || 'Could not save workflow')
    }
  }

  const saveAsNewWorkflow = async () => {
    if (nodes.length === 0) {
      setError('Nothing to save — drag nodes onto the canvas first.')
      return
    }
    const name = window.prompt('Save as new workflow:', '')?.trim()
    if (!name) return
    try {
      const r = await api.post<SavedWorkflow>('/api/analytics/workflows', buildSavePayload(name))
      setActiveSavedId(r.data.id)
      setActiveSavedName(r.data.name)
      loadSavedWorkflows()
    } catch (e: any) {
      setError(e?.response?.data?.detail || 'Could not save workflow')
    }
  }

  const loadSavedWorkflow = async (workflowId: string) => {
    try {
      const r = await api.get<SavedWorkflow>(`/api/analytics/workflows/${workflowId}`)
      // Replace canvas state in one shot.
      setNodes(r.data.nodes as any)
      setEdges(r.data.edges as any)
      setHorizon(r.data.horizon_months)
      setScenarioName(r.data.scenario_name || '')
      setStartDate(r.data.start_date || '')
      setView(r.data.view as WorkflowView)
      setActiveSavedId(r.data.id)
      setActiveSavedName(r.data.name)
      setError(null)
      setValidation(null)
      setRunError(null)
      setLastResult(null)
      setSavedMenuOpen(false)
    } catch (e: any) {
      setError(e?.response?.data?.detail || 'Could not load workflow')
    }
  }

  const deleteSavedWorkflow = async (workflowId: string, name: string) => {
    if (!window.confirm(`Delete saved workflow "${name}"? This can't be undone.`)) return
    try {
      await api.delete(`/api/analytics/workflows/${workflowId}`)
      // If the deleted one was the loaded record, detach.
      if (activeSavedId === workflowId) {
        setActiveSavedId(null)
        setActiveSavedName(null)
      }
      loadSavedWorkflows()
    } catch (e: any) {
      setError(e?.response?.data?.detail || 'Could not delete workflow')
    }
  }

  // ── Validate workflow ────────────────────────────────────────
  // First click runs the local + backend checks and renders the issue panel.
  // The "Ask the validator agent" button inside the panel hands off to chat
  // for deeper / open-ended troubleshooting.
  const runValidate = async () => {
    if (nodes.length === 0) {
      setValidation({
        ok: false,
        issues: [{
          severity: 'error',
          code: 'WORKFLOW_EMPTY',
          message: 'Workflow is empty.',
          hint: 'Drag a dataset and a model onto the canvas to get started.',
        }],
      })
      return
    }
    setValidating(true)
    try {
      const r = await api.post<WorkflowValidationResult>('/api/analytics/workflow-validate', {
        function_id: functionId,
        nodes: nodes.map((n) => ({
          id: n.id, kind: n.data.kind, ref_id: n.data.ref_id, config: n.data.config || {},
        })),
        edges: edges.map((e) => ({ source: e.source, target: e.target })),
      })
      setValidation(r.data)
    } catch (e: any) {
      setValidation({
        ok: false,
        issues: [{
          severity: 'error',
          code: 'VALIDATE_FAILED',
          message: 'Could not reach the validator.',
          hint: e?.response?.data?.detail || e?.message || 'Check your connection and try again.',
        }],
      })
    } finally {
      setValidating(false)
    }
  }

  const askValidatorAgent = () => {
    setEntity('workflow', null)
    setPayload({
      nodes: nodes.map((n) => ({
        id: n.id, kind: n.data.kind, ref_id: n.data.ref_id, config: n.data.config || {},
      })),
      edges: edges.map((e) => ({ source: e.source, target: e.target })),
      issues: validation?.issues ?? [],
    })
    setOpen(true)
    window.dispatchEvent(new CustomEvent('cma-chat', {
      detail: validation && validation.issues.length > 0
        ? `Help me fix these workflow issues: ${validation.issues.map((i) => i.message).join('; ')}`
        : 'Validate the workflow.',
    }))
  }

  const askRunErrorAgent = () => {
    if (!runError) return
    setEntity('workflow', null)
    setPayload({
      nodes: nodes.map((n) => ({
        id: n.id, kind: n.data.kind, ref_id: n.data.ref_id, config: n.data.config || {},
      })),
      edges: edges.map((e) => ({ source: e.source, target: e.target })),
      run_error: runError,
    })
    setOpen(true)
    window.dispatchEvent(new CustomEvent('cma-chat', {
      detail: `The workflow failed at step ${runError.step_index ?? '?'} (${runError.node_label ?? 'a model'}): ${runError.what_happened}. How do I fix this?`,
    }))
  }

  const highlightNode = (nodeId: string | null | undefined) => {
    if (!nodeId) return
    setNodes((ns) => ns.map((n) => ({ ...n, selected: n.id === nodeId })))
  }

  const askTroubleshooter = (run: AnalyticsRun) => {
    setEntity('run', run.id)
    setOpen(true)
    window.dispatchEvent(new CustomEvent('cma-chat', { detail: `Troubleshoot the failed run "${run.name}".` }))
  }

  const removeRun = async (id: string) => {
    if (!confirm('Delete this run?')) return
    await api.delete(`/api/analytics/runs/${id}`)
    if (activeRun?.id === id) setActiveRun(null)
    load()
  }

  // Group runs by workflow_id (most recent workflow first), one-off runs collapse to their own group
  const runGroups = useMemo(() => {
    const groups: { id: string; label: string; isWorkflow: boolean; runs: AnalyticsRun[]; created_at: string }[] = []
    const wfBuckets = new Map<string, AnalyticsRun[]>()
    const standalone: AnalyticsRun[] = []
    for (const r of runs) {
      if (r.workflow_id) {
        const bucket = wfBuckets.get(r.workflow_id) || []
        bucket.push(r)
        wfBuckets.set(r.workflow_id, bucket)
      } else {
        standalone.push(r)
      }
    }
    for (const [wfId, rs] of wfBuckets.entries()) {
      rs.sort((a, b) => (a.workflow_step_index ?? 0) - (b.workflow_step_index ?? 0))
      groups.push({
        id: wfId,
        label: `Workflow ${wfId.slice(-6)} · ${rs.length} step${rs.length === 1 ? '' : 's'}`,
        isWorkflow: true,
        runs: rs,
        created_at: rs[0]?.created_at || '',
      })
    }
    for (const r of standalone) {
      groups.push({
        id: r.id, label: r.name, isWorkflow: false, runs: [r], created_at: r.created_at,
      })
    }
    groups.sort((a, b) => b.created_at.localeCompare(a.created_at))
    return groups
  }, [runs])

  return (
    <div>
      {/* Header */}
      <div className="flex items-center justify-between mb-4 gap-3 flex-wrap">
        <div>
          <div className="font-display text-base font-semibold" style={{ color: 'var(--text-primary)' }}>
            Analytics Workflow Builder
          </div>
          <div className="text-xs flex items-center gap-1.5" style={{ color: 'var(--text-muted)' }}>
            <Hand size={11} />
            Drag pieces onto the canvas. Connect inputs into models. Models can chain. Models can take multiple inputs (merged on month).
          </div>
        </div>
        <button
          onClick={() => onAskAgent(`Suggest a workflow for ${functionName} given the available inputs and models.`)}
          className="px-3 py-2 rounded-lg text-xs font-semibold flex items-center gap-1.5"
          style={{ background: 'var(--bg-card)', border: '1px solid var(--border)', color: 'var(--text-secondary)' }}
        >
          <Sparkles size={13} /> Ask Agent
        </button>
      </div>

      {/* View switcher */}
      <div
        className="inline-flex p-1 rounded-lg mb-3"
        style={{ background: 'var(--bg-elevated)', border: '1px solid var(--border)' }}
      >
        {([
          { id: 'steps',  label: 'Steps',  icon: ListOrdered },
          { id: 'canvas', label: 'Canvas', icon: Layers },
          { id: 'spec',   label: 'Spec',   icon: FileCode },
        ] as const).map(({ id, label, icon: I }) => {
          const active = view === id
          return (
            <button
              key={id}
              onClick={() => setView(id as WorkflowView)}
              className="px-3 py-1.5 rounded-md text-xs font-semibold transition-colors flex items-center gap-1.5"
              style={{
                background: active ? 'var(--bg-card)' : 'transparent',
                color: active ? 'var(--accent)' : 'var(--text-secondary)',
                boxShadow: active ? '0 1px 4px rgba(0,0,0,0.05)' : 'none',
              }}
            >
              <I size={12} /> {label}
              {id === 'steps' && (
                <span
                  className="rounded-full px-1.5"
                  style={{ fontSize: 9, fontWeight: 700, background: active ? 'var(--accent-light)' : 'var(--bg-elevated)', color: active ? 'var(--accent)' : 'var(--text-muted)' }}
                >
                  {nodes.filter((n) => n.data.kind === 'model').length}
                </span>
              )}
            </button>
          )
        })}
      </div>

      {view === 'steps' && (
        <div className="mb-4">
          <WorkflowStepsView
            nodes={nodes}
            edges={edges}
            models={models}
            datasets={datasets}
            scenarios={scenarios}
            runs={runs}
            setNodes={setNodes as any}
            setEdges={setEdges as any}
            onConfigureDestination={(nodeId, kind) => setDestConfigFor({ nodeId, kind })}
          />
        </div>
      )}

      {view === 'spec' && (
        <div className="mb-4">
          <WorkflowSpecView
            nodes={nodes}
            edges={edges}
            horizon={horizon}
            models={models}
            datasets={datasets}
            scenarios={scenarios}
            onApply={(next) => {
              setNodes(() => next.nodes as any)
              setEdges(() => next.edges)
              setHorizon(next.horizon)
            }}
          />
        </div>
      )}

      {/* Canvas + palettes */}
      {view === 'canvas' && (
      <div className="grid grid-cols-1 lg:grid-cols-4 gap-3 mb-4" style={{ height: 620 }}>
        {/* Palette — scrolls independently of the canvas */}
        <div
          className="lg:col-span-1 space-y-3 overflow-y-auto pr-1"
          style={{ maxHeight: '100%' }}
        >
          <Palette title={`Datasets (${datasets.length})`} icon={Database} color="#059669"
            empty={datasets.length === 0 ? 'Bind a dataset on the Data tab.' : null}
          >
            {datasets.map((d) => (
              <PaletteCard
                key={d.id}
                title={d.name}
                subtitle={`${d.source_kind === 'upload' ? 'upload' : 'sql'} · ${d.columns.length} cols`}
                color="#059669"
                payload={{
                  kind: 'dataset', ref_id: d.id,
                  title: d.name,
                  subtitle: `${d.source_kind === 'upload' ? 'upload' : 'sql'} · ${d.columns.length} cols`,
                  color: '#059669',
                }}
                onClick={(p) => addFromClick(p)}
              />
            ))}
          </Palette>

          <Palette
            title={`Data Services (${transforms.length})`}
            icon={WorkflowIcon}
            color="#EA580C"
            empty={transforms.length === 0 ? 'No ETL transforms registered for this function yet.' : null}
          >
            {transforms.map((t) => {
              const src = t.input_data_source_ids
                .map((id) => dataSources.find((x) => x.id === id)?.name || id)
                .filter(Boolean)
              const sub = src.length ? `from ${src[0]}` : 'ETL recipe'
              return (
                <PaletteCard
                  key={t.id}
                  title={t.name}
                  subtitle={sub}
                  color="#EA580C"
                  payload={{
                    kind: 'transform', ref_id: t.id,
                    title: t.name,
                    subtitle: sub,
                    color: '#EA580C',
                  }}
                  onClick={(p) => addFromClick(p)}
                />
              )
            })}
          </Palette>

          <Palette title={`Scenarios (${scenarios.length})`} icon={FlaskConical} color="#0891B2">
            {scenarios.map((s) => (
              <PaletteCard
                key={s.id}
                title={s.name}
                subtitle={`${SEVERITY_LABEL[s.severity]} · ${s.variables.length} vars`}
                color={SEVERITY_COLOR[s.severity]}
                payload={{
                  kind: 'scenario', ref_id: s.id,
                  title: s.name,
                  subtitle: `${SEVERITY_LABEL[s.severity]} · ${s.variables.length} vars`,
                  color: SEVERITY_COLOR[s.severity],
                }}
                onClick={(p) => addFromClick(p)}
              />
            ))}
          </Palette>

          <Palette title={`Models (${models.length})`} icon={Boxes} color="#7C3AED"
            empty={models.length === 0 ? 'Build or upload a model on the Models tab.' : null}
          >
            {models.map((m) => (
              <PaletteCard
                key={m.id}
                title={m.name}
                subtitle={m.model_type.toUpperCase()}
                color="#7C3AED"
                payload={{
                  kind: 'model', ref_id: m.id,
                  title: m.name,
                  subtitle: m.model_type.toUpperCase(),
                  color: '#7C3AED',
                }}
                onClick={(p) => addFromClick(p)}
              />
            ))}
          </Palette>

          <Palette title="Destinations" icon={Download} color="#0F766E">
            {(Object.keys(DESTINATION_META) as DestinationKind[]).map((k) => {
              const meta = DESTINATION_META[k]
              return (
                <PaletteCard
                  key={k}
                  title={meta.label}
                  subtitle="configure on drop"
                  color={meta.color}
                  payload={{
                    kind: 'destination', ref_id: k,
                    title: meta.label,
                    subtitle: 'unconfigured',
                    color: meta.color,
                  }}
                  onClick={(p) => addFromClick(p)}
                />
              )
            })}
          </Palette>
        </div>

        {/* Canvas — fixed height, doesn't grow with palette content */}
        <div
          className="lg:col-span-3"
          ref={flowRef}
          style={{
            background: 'var(--bg-card)',
            border: '1px solid var(--border)',
            borderRadius: 12,
            overflow: 'hidden',
            position: 'relative',
            height: '100%',
          }}
        >
          <ReactFlow
            nodes={nodes}
            edges={edges}
            onNodesChange={onNodesChange}
            onEdgesChange={onEdgesChange}
            onConnect={onConnect}
            isValidConnection={isValidConnection}
            onDragOver={onDragOver}
            onDrop={onDrop}
            onNodeClick={(_e, n) => {
              const data = n.data as NodeData
              if (data.kind === 'destination') {
                setDestConfigFor({ nodeId: n.id, kind: data.ref_id as DestinationKind })
              } else if (data.kind === 'transform') {
                setTransformPanelFor(n.id)
              }
            }}
            nodeTypes={NODE_TYPES}
            connectionMode={ConnectionMode.Loose}
            fitView
            fitViewOptions={{ padding: 0.2 }}
            proOptions={{ hideAttribution: true }}
          >
            <Background gap={16} size={1} color="rgba(0,0,0,0.06)" />
            <Controls position="bottom-right" showInteractive={false} />
            {nodes.length > 4 && <MiniMap pannable zoomable position="bottom-left" style={{ width: 120, height: 80 }} />}
          </ReactFlow>

          {nodes.length === 0 && (
            <div
              className="absolute inset-0 flex items-center justify-center pointer-events-none"
              style={{ color: 'var(--text-muted)' }}
            >
              <div className="text-center">
                <Layers size={32} style={{ margin: '0 auto 8px', opacity: 0.4 }} />
                <div className="text-sm font-semibold">Empty canvas</div>
                <div className="text-xs mt-0.5">
                  Drag a dataset or scenario from the palette → connect into a model.
                </div>
              </div>
            </div>
          )}
        </div>
      </div>
      )}

      {/* Validation feedback */}
      {validation && (
        <WorkflowValidationPanel
          result={validation}
          onClose={() => setValidation(null)}
          onHighlight={highlightNode}
          onAskAgent={askValidatorAgent}
          onRevalidate={runValidate}
          revalidating={validating}
        />
      )}

      {/* Run-time failure card */}
      {runError && (
        <WorkflowRunErrorCard
          detail={runError}
          onClose={() => setRunError(null)}
          onHighlight={highlightNode}
          onAskAgent={askRunErrorAgent}
        />
      )}

      {/* Status strip — most recent workflow result */}
      {lastResult && (
        <ResultStatusStrip
          result={lastResult}
          onClear={() => setLastResult(null)}
        />
      )}

      {/* Run controls */}
      <div className="panel mb-6 flex items-end gap-3 flex-wrap" style={{ padding: 14 }}>
        <Field label="Horizon (months)" className="w-28">
          <input
            type="number" min={1} max={500} className="input"
            value={horizon}
            onChange={(e) => setHorizon(Math.max(1, Math.min(500, parseInt(e.target.value || '27'))))}
          />
        </Field>
        <Field label="Scenario" className="w-52">
          <select
            className="input"
            value={scenarioName}
            onChange={(e) => setScenarioName(e.target.value)}
            title="CCAR + Outlook scenarios from the Data tab"
          >
            <option value="">— none —</option>
            {scenarios.map((s) => (
              <option key={s.id} value={s.name}>{s.name}</option>
            ))}
          </select>
        </Field>
        <Field label="Start date" className="w-40">
          <input
            type="date"
            className="input"
            value={startDate}
            onChange={(e) => setStartDate(e.target.value)}
          />
        </Field>
        <div className="flex-1 min-w-[200px]">
          <div className="text-[11px] font-semibold uppercase tracking-widest mb-1" style={{ color: 'var(--text-secondary)' }}>
            Workflow
          </div>
          <div className="text-xs" style={{ color: 'var(--text-muted)' }}>
            {nodes.length} node{nodes.length === 1 ? '' : 's'}
            {' · '}
            {nodes.filter((n) => n.data.kind === 'model').length} model{nodes.filter((n) => n.data.kind === 'model').length === 1 ? '' : 's'}
            {' · '}
            {edges.length} edge{edges.length === 1 ? '' : 's'}
          </div>
        </div>
        <div className="flex gap-2 flex-wrap">
          <SavedWorkflowsMenu
            saved={savedWorkflows}
            activeSavedId={activeSavedId}
            activeSavedName={activeSavedName}
            open={savedMenuOpen}
            onToggle={() => setSavedMenuOpen((o) => !o)}
            onClose={() => setSavedMenuOpen(false)}
            onLoad={loadSavedWorkflow}
            onDelete={deleteSavedWorkflow}
          />
          <button
            onClick={saveCurrentWorkflow}
            disabled={nodes.length === 0}
            className="px-3 py-2 rounded-lg text-xs font-semibold disabled:opacity-40 flex items-center gap-1.5"
            style={{ background: 'var(--bg-card)', border: '1px solid #2563EB', color: '#2563EB' }}
            title={activeSavedId ? `Update saved workflow "${activeSavedName}"` : 'Save current workflow'}
          >
            <Save size={13} /> {activeSavedId ? 'Save' : 'Save…'}
          </button>
          {activeSavedId && (
            <button
              onClick={saveAsNewWorkflow}
              disabled={nodes.length === 0}
              className="px-3 py-2 rounded-lg text-xs font-semibold disabled:opacity-40"
              style={{ background: 'var(--bg-elevated)', border: '1px solid var(--border)', color: 'var(--text-secondary)' }}
              title="Save as a new workflow record (don't overwrite the loaded one)"
            >
              Save as…
            </button>
          )}
          <button
            onClick={runValidate}
            disabled={nodes.length === 0 || validating}
            className="px-3 py-2 rounded-lg text-xs font-semibold disabled:opacity-40 flex items-center gap-1.5"
            style={{ background: 'var(--bg-card)', border: '1px solid #D97706', color: '#D97706' }}
            title="Check the workflow for missing inputs, mismatched features, and configuration gaps"
          >
            {validating ? <Loader2 size={13} className="animate-spin" /> : '✓'}
            {validating ? 'Validating…' : 'Validate'}
          </button>
          <button
            onClick={clearCanvas}
            disabled={nodes.length === 0 && edges.length === 0}
            className="px-3 py-2 rounded-lg text-xs font-semibold disabled:opacity-40"
            style={{ background: 'var(--bg-elevated)', border: '1px solid var(--border)', color: 'var(--text-secondary)' }}
          >
            Clear
          </button>
          <button
            onClick={submit}
            disabled={nodes.length === 0 || running}
            className="px-4 py-2 rounded-lg text-xs font-semibold flex items-center gap-1.5 disabled:opacity-40"
            style={{ background: 'var(--accent)', color: '#fff' }}
          >
            <Play size={13} /> {running ? 'Running…' : 'Run Workflow'}
          </button>
        </div>
        {error && (
          <div className="w-full text-xs px-3 py-2 rounded-md" style={{ background: 'var(--error-bg)', color: 'var(--error)' }}>
            {error}
          </div>
        )}
      </div>

      {/* Run history grouped by workflow */}
      <div>
        <div className="section-title">Run History ({runs.length})</div>
        {runs.length === 0 ? (
          <div className="panel text-center" style={{ padding: '32px 20px', borderStyle: 'dashed' }}>
            <FlaskConical size={24} style={{ color: 'var(--text-muted)', margin: '0 auto 10px' }} />
            <div className="text-sm font-semibold" style={{ color: 'var(--text-primary)' }}>
              No runs yet
            </div>
            <div className="text-xs mt-1" style={{ color: 'var(--text-muted)' }}>
              Compose a workflow above and hit Run.
            </div>
          </div>
        ) : (
          <div className="space-y-3">
            {runGroups.map((g) => (
              <RunGroup
                key={g.id}
                group={g}
                isLatest={g.id === latestWorkflowId}
                models={models}
                scenarios={scenarios}
                datasets={datasets}
                allNodes={nodes}
                onOpen={setActiveRun}
                onDelete={removeRun}
                onAskAgent={onAskAgent}
                onTroubleshoot={askTroubleshooter}
              />
            ))}
          </div>
        )}
      </div>

      {activeRun && (
        <RunDetailPanel
          run={activeRun}
          model={models.find((m) => m.id === activeRun.model_id)}
          scenario={scenarios.find((s) => s.id === activeRun.scenario_id)}
          dataset={datasets.find((d) => d.id === activeRun.dataset_id)}
          onClose={() => setActiveRun(null)}
          onAskAgent={onAskAgent}
        />
      )}

      {destConfigFor && (() => {
        const target = nodes.find((n) => n.id === destConfigFor.nodeId)
        if (!target) return null
        return (
          <DestinationConfigModal
            kind={destConfigFor.kind}
            initial={target.data.config || {}}
            onClose={() => setDestConfigFor(null)}
            onSave={(cfg, subtitle) => {
              updateNodeConfig(destConfigFor.nodeId, cfg, subtitle)
              setDestConfigFor(null)
            }}
          />
        )
      })()}

      {transformPanelFor && (() => {
        const node = nodes.find((n) => n.id === transformPanelFor)
        if (!node) return null
        const t = transforms.find((x) => x.id === node.data.ref_id)
        if (!t) return null
        const sourceNames = t.input_data_source_ids
          .map((id) => dataSources.find((x) => x.id === id)?.name || id)
          .filter(Boolean)
        const outputDs = datasets.find((d) => d.id === t.output_dataset_id)
        return (
          <TransformInspector
            node={node}
            transform={t}
            datasets={datasets}
            models={models}
            sourceNames={sourceNames}
            outputDatasetName={outputDs?.name || t.output_dataset_id || '(no output dataset)'}
            onSave={(cfg, subtitle) => updateNodeConfig(node.id, cfg, subtitle)}
            onRemove={() => {
              setNodes((ns) => ns.filter((n) => n.id !== node.id))
              setEdges((es) => es.filter((e) => e.source !== node.id && e.target !== node.id))
              setTransformPanelFor(null)
            }}
            onClose={() => setTransformPanelFor(null)}
          />
        )
      })()}

      <style>{`
        .input {
          width: 100%; padding: 8px 10px; border-radius: 8px; font-size: 13px;
          background: var(--bg-elevated); border: 1px solid var(--border); color: var(--text-primary);
        }
        .react-flow__handle {
          width: 10px; height: 10px;
          border: 2px solid var(--bg-card);
          background: var(--accent);
        }
        .react-flow__edge-path { stroke-width: 1.5; }
      `}</style>
    </div>
  )
}

// ── Custom node renderers ───────────────────────────────────────────
function DatasetNode({ data, selected }: NodeProps) {
  const d = data as NodeData
  return <NodeCard data={d} selected={!!selected} icon={Database} />
}
function ScenarioNode({ data, selected }: NodeProps) {
  const d = data as NodeData
  return <NodeCard data={d} selected={!!selected} icon={FlaskConical} />
}
function ModelNode({ data, selected }: NodeProps) {
  const d = data as NodeData
  return <NodeCard data={d} selected={!!selected} icon={Boxes} />
}
function DestinationNode({ data, selected }: NodeProps) {
  const d = data as NodeData
  const meta = DESTINATION_META[d.ref_id as DestinationKind]
  return <NodeCard data={d} selected={!!selected} icon={meta?.icon || Download} />
}
function TransformNode({ data, selected }: NodeProps) {
  const d = data as NodeData
  return <NodeCard data={d} selected={!!selected} icon={WorkflowIcon} />
}

const NODE_TYPES = {
  dataset: DatasetNode,
  scenario: ScenarioNode,
  model: ModelNode,
  destination: DestinationNode,
  transform: TransformNode,
}

function NodeCard({
  data, selected, icon: Icon,
}: {
  data: NodeData
  selected: boolean
  icon: any
}) {
  const status = data.status || 'idle'
  const statusColor = STATUS_COLOR[status]
  const isUnconfiguredDest = data.kind === 'destination' && !nodeHasConfig(data)
  // Hide subtitle for the two transform boxes whose name already speaks
  // for itself — the "from <dataset>" / "ETL recipe" hint is noise here.
  const hideSubtitle = data.title === 'Data Harness' || data.title === 'Data Quality Check'
  return (
    <div
      style={{
        background: 'var(--bg-card)',
        border: `1.5px solid ${selected ? 'var(--accent)' : isUnconfiguredDest ? 'var(--warning)' : data.color}`,
        borderRadius: 10,
        padding: '8px 12px',
        minWidth: 180,
        position: 'relative',
        boxShadow: selected ? `0 6px 20px ${data.color}33` : '0 2px 8px rgba(0,0,0,0.06)',
      }}
    >
      {/* Four connection handles — one per edge — so wires can run
          horizontally OR vertically. With connectionMode="loose" any
          handle can be source or target; isValidConnection still
          enforces which node *kinds* may receive incoming edges. */}
      <Handle type="source" position={Position.Top}    id="t" />
      <Handle type="source" position={Position.Right}  id="r" />
      <Handle type="source" position={Position.Bottom} id="b" />
      <Handle type="source" position={Position.Left}   id="l" />

      {/* Status indicator dot */}
      {status !== 'idle' && (
        <div
          style={{
            position: 'absolute', top: -4, right: -4,
            width: 12, height: 12, borderRadius: 6,
            background: statusColor,
            border: '2px solid var(--bg-card)',
            display: 'flex', alignItems: 'center', justifyContent: 'center',
          }}
        >
          {status === 'running' && <Loader2 size={8} className="animate-spin" color="#fff" />}
        </div>
      )}

      <div className="flex items-center gap-2">
        <div
          className="w-7 h-7 rounded-md flex items-center justify-center shrink-0"
          style={{ background: `${data.color}1A`, color: data.color }}
        >
          <Icon size={14} />
        </div>
        <div className="min-w-0 flex-1">
          <div className="text-[12px] font-semibold truncate" style={{ color: 'var(--text-primary)' }}>
            {data.title}
          </div>
          <div
            className="text-[10px] truncate font-mono"
            style={{
              color: isUnconfiguredDest ? 'var(--warning)' : 'var(--text-muted)',
              fontWeight: isUnconfiguredDest ? 600 : 400,
            }}
          >
            {isUnconfiguredDest
              ? '⚠ click to configure'
              : hideSubtitle ? '' : data.subtitle}
          </div>
        </div>
      </div>
    </div>
  )
}

function nodeHasConfig(data: NodeData): boolean {
  if (data.kind !== 'destination') return true
  const c = data.config || {}
  return !!(c.table || c.bucket || c.filename || c.ref)
}

function downloadCsv(filename: string, rows: Record<string, any>[]) {
  if (rows.length === 0) return
  const headers = Object.keys(rows[0])
  const escape = (v: any) => {
    if (v == null) return ''
    const s = typeof v === 'object' ? JSON.stringify(v) : String(v)
    return /[",\n]/.test(s) ? `"${s.replace(/"/g, '""')}"` : s
  }
  const lines = [headers.join(','), ...rows.map((r) => headers.map((h) => escape(r[h])).join(','))]
  const blob = new Blob([lines.join('\n')], { type: 'text/csv;charset=utf-8' })
  const url = URL.createObjectURL(blob)
  const a = document.createElement('a')
  a.href = url
  a.download = filename
  document.body.appendChild(a)
  a.click()
  document.body.removeChild(a)
  URL.revokeObjectURL(url)
}

// ── Palette ────────────────────────────────────────────────────────────
function Palette({
  title, icon: Icon, color, empty, children,
}: {
  title: string
  icon: any
  color: string
  empty?: string | null
  children: React.ReactNode
}) {
  return (
    <div className="panel" style={{ padding: 12 }}>
      <div className="flex items-center gap-1.5 mb-2">
        <div
          className="w-6 h-6 rounded-md flex items-center justify-center"
          style={{ background: `${color}1A`, color }}
        >
          <Icon size={11} />
        </div>
        <span style={{
          fontSize: 10, fontWeight: 700, letterSpacing: '0.10em',
          color: 'var(--text-secondary)', textTransform: 'uppercase',
        }}>
          {title}
        </span>
      </div>
      <div className="space-y-1.5">
        {empty && <div className="text-[11px]" style={{ color: 'var(--text-muted)' }}>{empty}</div>}
        {children}
      </div>
    </div>
  )
}

function PaletteCard({
  title, subtitle, color, payload, onClick,
}: {
  title: string
  subtitle: string
  color: string
  payload: NodeData
  onClick: (p: NodeData) => void
}) {
  return (
    <div
      draggable
      onDragStart={(e) => {
        e.dataTransfer.setData(PALETTE_DRAG_MIME, JSON.stringify(payload))
        e.dataTransfer.effectAllowed = 'copy'
      }}
      onClick={() => onClick(payload)}
      className="rounded-md transition-all cursor-grab active:cursor-grabbing"
      style={{
        padding: '6px 8px',
        background: 'var(--bg-elevated)',
        border: '1px solid var(--border)',
        userSelect: 'none',
      }}
      onMouseEnter={(e) => { (e.currentTarget as HTMLElement).style.borderColor = color }}
      onMouseLeave={(e) => { (e.currentTarget as HTMLElement).style.borderColor = 'var(--border)' }}
    >
      <div className="flex items-center gap-2">
        <GripVertical size={11} style={{ color: 'var(--text-muted)', flexShrink: 0 }} />
        <div className="min-w-0 flex-1">
          <div className="text-[12px] font-semibold truncate" style={{ color: 'var(--text-primary)' }}>
            {title}
          </div>
          <div className="text-[10px] truncate font-mono" style={{ color: 'var(--text-muted)' }}>
            {subtitle}
          </div>
        </div>
      </div>
    </div>
  )
}

// ── Run group / row / detail (unchanged from before, with light edits) ──
function RunGroup({
  group, isLatest, models, scenarios, datasets, allNodes, onOpen, onDelete, onAskAgent, onTroubleshoot,
}: {
  group: { id: string; label: string; isWorkflow: boolean; runs: AnalyticsRun[] }
  isLatest: boolean
  models: TrainedModel[]
  scenarios: Scenario[]
  datasets: Dataset[]
  allNodes: RFNode<NodeData>[]
  onOpen: (r: AnalyticsRun) => void
  onDelete: (id: string) => void
  onAskAgent: (q: string) => void
  onTroubleshoot: (r: AnalyticsRun) => void
}) {
  return (
    <div
      className="panel"
      style={{
        padding: 8,
        borderColor: isLatest ? 'var(--accent)' : 'var(--border)',
      }}
    >
      {group.isWorkflow && (
        <div
          className="flex items-center gap-2 px-2 py-1 mb-1"
          style={{
            fontSize: 10, fontWeight: 700, letterSpacing: '0.08em',
            color: 'var(--text-secondary)', textTransform: 'uppercase',
          }}
        >
          <Layers size={11} />
          {group.label}
          {isLatest && (
            <span
              className="pill ml-1"
              style={{ fontSize: 9, background: 'var(--accent-light)', color: 'var(--accent)', borderColor: 'transparent' }}
            >
              JUST RAN
            </span>
          )}
        </div>
      )}
      <div className="space-y-1">
        {group.runs.map((r) => (
          <RunRow
            key={r.id}
            run={r}
            modelName={models.find((m) => m.id === r.model_id)?.name}
            inputLabels={(r.input_node_ids || []).map((nid) => {
              // Try to find in current canvas nodes
              const cn = allNodes.find((n) => n.id === nid)
              if (cn) return cn.data.title
              return nid
            })}
            onOpen={() => onOpen(r)}
            onDelete={() => onDelete(r.id)}
            onTroubleshoot={() => onTroubleshoot(r)}
            onAskAgent={onAskAgent}
            scenarios={scenarios}
            datasets={datasets}
          />
        ))}
      </div>
    </div>
  )
}

function RunRow({
  run, modelName, inputLabels, onOpen, onDelete, onAskAgent, onTroubleshoot, scenarios, datasets,
}: {
  run: AnalyticsRun
  modelName?: string
  inputLabels: string[]
  onOpen: () => void
  onDelete: () => void
  onAskAgent: (q: string) => void
  onTroubleshoot: () => void
  scenarios: Scenario[]
  datasets: Dataset[]
}) {
  const ok = run.status === 'completed'
  const sevColor = run.scenario_id ? SEVERITY_COLOR[scenarios.find((s) => s.id === run.scenario_id)?.severity || 'custom'] : 'var(--accent)'
  const isWorkflow = run.input_kind === 'workflow'
  const inputDesc = isWorkflow
    ? (inputLabels.length > 0 ? inputLabels.join(' + ') : 'workflow input')
    : run.scenario_id
      ? scenarios.find((s) => s.id === run.scenario_id)?.name || run.scenario_id
      : datasets.find((d) => d.id === run.dataset_id)?.name || run.dataset_id

  return (
    <div
      className="cursor-pointer transition-all rounded-md"
      style={{ padding: 10, background: 'var(--bg-card)', border: '1px solid transparent' }}
      onClick={onOpen}
      onMouseEnter={(e) => ((e.currentTarget as HTMLElement).style.borderColor = 'var(--accent)')}
      onMouseLeave={(e) => ((e.currentTarget as HTMLElement).style.borderColor = 'transparent')}
    >
      <div className="flex items-center gap-3">
        <div
          className="w-8 h-8 rounded-lg flex items-center justify-center shrink-0"
          style={{ background: ok ? `${sevColor}1A` : 'var(--error-bg)', color: ok ? sevColor : 'var(--error)' }}
        >
          {ok ? <CheckCircle2 size={14} /> : <AlertCircle size={14} />}
        </div>
        <div className="min-w-0 flex-1">
          <div className="font-semibold text-[13px] truncate" style={{ color: 'var(--text-primary)' }}>
            {run.name}
          </div>
          <div className="text-[11px] truncate font-mono" style={{ color: 'var(--text-muted)' }}>
            {modelName || run.model_id} ← {inputDesc} · {run.horizon_months}m
          </div>
        </div>
        <div className="text-right shrink-0 hidden md:block">
          {ok && run.summary?.mean_prediction != null && (
            <div className="font-mono text-[12px]" style={{ color: 'var(--text-primary)' }}>
              μ {Number(run.summary.mean_prediction).toFixed(3)}
            </div>
          )}
          <div className="text-[10px]" style={{ color: 'var(--text-muted)' }}>
            {run.duration_ms.toFixed(0)}ms
          </div>
        </div>
        <div className="flex gap-1 shrink-0" onClick={(e) => e.stopPropagation()}>
          {!ok && (
            <button
              onClick={onTroubleshoot}
              className="px-2 py-1 rounded-md text-[10px] font-bold flex items-center gap-1"
              style={{ background: 'var(--error-bg)', color: 'var(--error)' }}
              title="Ask the Run Troubleshooter"
            >
              <AlertCircle size={11} /> TROUBLESHOOT
            </button>
          )}
          <button onClick={() => onAskAgent(`Interpret the run "${run.name}".`)}
            className="p-1.5 rounded-md" style={{ color: 'var(--text-muted)' }}>
            <Sparkles size={12} />
          </button>
          <button onClick={onDelete} className="p-1.5 rounded-md" style={{ color: 'var(--text-muted)' }}>
            <Trash2 size={12} />
          </button>
          <button onClick={onOpen} className="p-1.5 rounded-md" style={{ color: 'var(--text-muted)' }}>
            <ChevronRight size={12} />
          </button>
        </div>
      </div>
    </div>
  )
}

// ── Run detail panel ──────────────────────────────────────────────────
function RunDetailPanel({
  run, model, scenario, dataset, onClose, onAskAgent,
}: {
  run: AnalyticsRun
  model?: TrainedModel
  scenario?: Scenario
  dataset?: Dataset
  onClose: () => void
  onAskAgent: (q: string) => void
}) {
  const COLORS = ['#004977', '#059669', '#D97706', '#DC2626', '#7C3AED', '#0891B2']
  const allKeys = run.series.length > 0 ? Object.keys(run.series[0]).filter((k) => k !== 'month') : []
  const driverKeys = allKeys.filter((k) => k !== 'prediction').slice(0, 5)

  const inputLabel =
    run.input_kind === 'workflow'
      ? `Workflow input (${(run.input_node_ids || []).length} sources)`
      : run.input_kind === 'scenario'
        ? `${scenario?.name || run.scenario_id} (scenario)`
        : `${dataset?.name || run.dataset_id} (dataset)`

  return (
    <SidePanel
      title={run.name}
      subtitle={`${model?.name || run.model_id} × ${inputLabel} · ${run.horizon_months}m · ${run.duration_ms.toFixed(0)}ms`}
      onClose={onClose}
    >
      {run.status === 'failed' && (
        <div className="text-xs px-3 py-2 rounded-md" style={{ background: 'var(--error-bg)', color: 'var(--error)' }}>
          <strong>Run failed:</strong> {run.error || 'Unknown error'}
        </div>
      )}
      {run.status === 'completed' && (
        <>
          <div className="grid grid-cols-3 gap-2">
            {Object.entries(run.summary)
              .filter(([k]) => ['min_prediction', 'mean_prediction', 'max_prediction'].includes(k))
              .map(([k, v]) => (
                <div key={k} className="panel" style={{ padding: 12 }}>
                  <div className="metric-label" style={{ fontSize: 9 }}>
                    {k.replace('_prediction', '').toUpperCase()}
                  </div>
                  <div className="metric-value mt-1" style={{ fontSize: 18 }}>
                    {typeof v === 'number' ? v.toFixed(4) : v}
                  </div>
                </div>
              ))}
          </div>

          {(run.summary.features_unmatched as string[] | undefined)?.length ? (
            <div className="text-[11px] px-3 py-2 rounded-md flex items-start gap-2"
              style={{ background: 'var(--warning-bg)', color: 'var(--warning)' }}
            >
              <AlertCircle size={12} className="mt-0.5 shrink-0" />
              <span>
                {String((run.summary.features_unmatched as string[]).length)} feature(s) had no match in the input:{' '}
                <code>{(run.summary.features_unmatched as string[]).join(', ')}</code>. They contributed only the intercept.
              </span>
            </div>
          ) : null}

          <div>
            <div className="flex items-center justify-between mb-2">
              <div className="section-title" style={{ marginBottom: 0 }}>Prediction Path</div>
              <button
                onClick={() => onAskAgent(`Interpret the analytics run "${run.name}" — model output by horizon.`)}
                className="text-[11px] flex items-center gap-1"
                style={{ color: 'var(--accent)', fontWeight: 600 }}
              >
                <Sparkles size={11} /> Interpret
              </button>
            </div>
            <div className="panel" style={{ padding: 14 }}>
              <ResponsiveContainer width="100%" height={220}>
                <LineChart data={run.series} margin={{ top: 8, right: 12, left: -8, bottom: 0 }}>
                  <CartesianGrid strokeDasharray="3 3" stroke="var(--border-subtle)" />
                  <XAxis dataKey="month" tick={{ fontSize: 11, fill: 'var(--text-muted)' }} />
                  <YAxis tick={{ fontSize: 11, fill: 'var(--text-muted)' }} />
                  <RTooltip contentStyle={tooltipStyle} />
                  <Line type="monotone" dataKey="prediction" stroke={COLORS[0]} strokeWidth={3} dot={{ r: 3 }} activeDot={{ r: 6 }} />
                </LineChart>
              </ResponsiveContainer>
            </div>
          </div>

          {driverKeys.length > 0 && (
            <div>
              <div className="section-title">
                {run.input_kind === 'scenario' ? 'Macro Drivers' : 'Input Features'}
              </div>
              <div className="panel" style={{ padding: 14 }}>
                <ResponsiveContainer width="100%" height={200}>
                  <LineChart data={run.series} margin={{ top: 8, right: 12, left: -8, bottom: 0 }}>
                    <CartesianGrid strokeDasharray="3 3" stroke="var(--border-subtle)" />
                    <XAxis dataKey="month" tick={{ fontSize: 11, fill: 'var(--text-muted)' }} />
                    <YAxis tick={{ fontSize: 11, fill: 'var(--text-muted)' }} />
                    <RTooltip contentStyle={tooltipStyle} />
                    <Legend wrapperStyle={{ fontSize: 11 }} />
                    {driverKeys.map((k, i) => (
                      <Line key={k} type="monotone" dataKey={k} stroke={COLORS[(i + 1) % COLORS.length]} strokeWidth={2} dot={{ r: 2 }} />
                    ))}
                  </LineChart>
                </ResponsiveContainer>
              </div>
            </div>
          )}

          <div>
            <div className="section-title">Series ({run.series.length} rows)</div>
            <div className="overflow-auto rounded-lg" style={{ border: '1px solid var(--border)', maxHeight: 260 }}>
              <table className="w-full text-xs font-mono">
                <thead style={{ background: 'var(--bg-elevated)', position: 'sticky', top: 0 }}>
                  <tr>
                    {['month', 'prediction', ...driverKeys].map((k) => (
                      <th key={k} className="text-left py-2 px-3 whitespace-nowrap" style={{
                        fontSize: 10, fontWeight: 700, letterSpacing: '0.05em',
                        textTransform: 'uppercase', color: 'var(--text-secondary)',
                        borderBottom: '1px solid var(--border)',
                      }}>
                        {k}
                      </th>
                    ))}
                  </tr>
                </thead>
                <tbody>
                  {run.series.map((row, i) => (
                    <tr key={i}>
                      {['month', 'prediction', ...driverKeys].map((k) => (
                        <td key={k} className="py-1.5 px-3" style={{
                          borderBottom: '1px solid var(--border-subtle)',
                          color: k === 'prediction' ? 'var(--accent)' : undefined,
                          fontWeight: k === 'prediction' ? 600 : 400,
                        }}>
                          {typeof row[k] === 'number' ? row[k].toFixed(4) : String(row[k] ?? '—')}
                        </td>
                      ))}
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          </div>
          {run.notes && (
            <div className="text-xs" style={{ color: 'var(--text-secondary)' }}>
              <strong>Notes:</strong> {run.notes}
            </div>
          )}
        </>
      )}
    </SidePanel>
  )
}

// ── Shared chrome ─────────────────────────────────────────────────────
function SidePanel({
  title, subtitle, onClose, children,
}: {
  title: string
  subtitle?: string
  onClose: () => void
  children: React.ReactNode
}) {
  return (
    <>
      <div className="fixed inset-0 z-40" style={{ background: 'rgba(11,15,25,0.45)' }} onClick={onClose} />
      <div
        className="fixed top-0 right-0 bottom-0 z-50 flex flex-col"
        style={{
          width: 'min(880px, 96vw)',
          background: 'var(--bg-card)', borderLeft: '1px solid var(--border)',
          boxShadow: '-12px 0 48px rgba(0,0,0,0.18)',
        }}
      >
        <div
          className="flex items-center justify-between px-5 py-4"
          style={{
            background: 'linear-gradient(135deg, var(--accent), var(--teal))',
            borderBottom: '1px solid var(--border)',
          }}
        >
          <div>
            <div className="font-display text-base font-semibold" style={{ color: '#fff' }}>{title}</div>
            {subtitle && (
              <div className="font-mono" style={{ fontSize: 11, color: 'rgba(255,255,255,0.75)' }}>{subtitle}</div>
            )}
          </div>
          <button onClick={onClose} className="p-1.5 rounded-lg" style={{ color: 'rgba(255,255,255,0.85)' }}>
            <X size={16} />
          </button>
        </div>
        <div className="flex-1 overflow-y-auto px-5 py-4 space-y-4">{children}</div>
      </div>
    </>
  )
}

function Field({ label, children, className = '' }: { label: string; children: React.ReactNode; className?: string }) {
  return (
    <label className={`block ${className}`}>
      <span className="block text-[11px] font-semibold uppercase tracking-widest mb-1" style={{ color: 'var(--text-secondary)' }}>
        {label}
      </span>
      {children}
    </label>
  )
}

const tooltipStyle = {
  background: 'var(--bg-card)',
  border: '1px solid var(--border)',
  borderRadius: 8,
  fontSize: 11,
  fontFamily: 'JetBrains Mono, monospace',
  color: 'var(--text-primary)',
}

// ── Validation panel ──────────────────────────────────────────────────
const SEVERITY_TONE: Record<WorkflowValidationIssue['severity'], { color: string; bg: string; icon: typeof AlertCircle; label: string }> = {
  error:   { color: 'var(--error)',   bg: 'var(--error-bg)',   icon: AlertCircle,   label: 'BLOCKER' },
  warning: { color: 'var(--warning)', bg: 'var(--warning-bg)', icon: AlertCircle,   label: 'WARNING' },
  info:    { color: 'var(--accent)',  bg: 'var(--accent-light)', icon: Sparkles,    label: 'NOTE'    },
}

// ── Saved-workflows dropdown ────────────────────────────────────────────
function SavedWorkflowsMenu({
  saved, activeSavedId, activeSavedName, open,
  onToggle, onClose, onLoad, onDelete,
}: {
  saved: SavedWorkflowSummary[]
  activeSavedId: string | null
  activeSavedName: string | null
  open: boolean
  onToggle: () => void
  onClose: () => void
  onLoad: (id: string) => void
  onDelete: (id: string, name: string) => void
}) {
  const label = activeSavedName || 'Saved workflows'
  return (
    <div className="relative">
      <button
        onClick={onToggle}
        className="px-3 py-2 rounded-lg text-xs font-semibold flex items-center gap-1.5"
        style={{
          background: 'var(--bg-card)',
          border: '1px solid var(--border)',
          color: 'var(--text-primary)',
          minWidth: 150,
        }}
        title="Saved workflows for this function"
      >
        <Bookmark size={13} />
        <span className="truncate" style={{ maxWidth: 180 }}>{label}</span>
        <span className="font-mono ml-auto" style={{ fontSize: 10, color: 'var(--text-muted)' }}>
          {saved.length}
        </span>
      </button>
      {open && (
        <>
          {/* Backdrop to dismiss on outside click */}
          <div
            onClick={onClose}
            style={{ position: 'fixed', inset: 0, zIndex: 40 }}
          />
          <div
            className="rounded-lg overflow-hidden"
            style={{
              position: 'absolute',
              top: 'calc(100% + 4px)',
              left: 0,
              minWidth: 320,
              maxWidth: 420,
              maxHeight: 360,
              overflowY: 'auto',
              background: 'var(--bg-card)',
              border: '1px solid var(--border)',
              boxShadow: '0 8px 24px rgba(0,0,0,0.10)',
              zIndex: 50,
            }}
          >
            {saved.length === 0 ? (
              <div
                className="px-3 py-4 text-xs text-center"
                style={{ color: 'var(--text-muted)' }}
              >
                No saved workflows yet. Build one and click Save to add the first.
              </div>
            ) : (
              <ul className="divide-y" style={{ borderColor: 'var(--border-subtle)' }}>
                {saved.map((s) => {
                  const isActive = s.id === activeSavedId
                  return (
                    <li
                      key={s.id}
                      className="px-3 py-2 flex items-start gap-2 group hover:bg-[var(--bg-elevated)] cursor-default"
                      style={{ background: isActive ? 'var(--accent-light)' : 'transparent' }}
                    >
                      <button
                        onClick={() => onLoad(s.id)}
                        className="flex-1 text-left min-w-0"
                      >
                        <div
                          className="text-[12px] font-semibold truncate"
                          style={{ color: isActive ? 'var(--accent)' : 'var(--text-primary)' }}
                        >
                          {s.name}
                          {isActive && <span className="ml-1 font-mono text-[9px]">· loaded</span>}
                        </div>
                        <div className="text-[10px] font-mono" style={{ color: 'var(--text-muted)' }}>
                          {s.node_count} node{s.node_count === 1 ? '' : 's'} · {s.edge_count} edge{s.edge_count === 1 ? '' : 's'}
                          {' · '}
                          {(s.updated_at || s.created_at).slice(0, 10)}
                        </div>
                      </button>
                      <button
                        onClick={(e) => { e.stopPropagation(); onDelete(s.id, s.name) }}
                        className="p-1 rounded-md opacity-0 group-hover:opacity-100 transition-opacity"
                        style={{ color: 'var(--text-muted)' }}
                        title="Delete this saved workflow"
                        onMouseEnter={(e) => ((e.currentTarget as HTMLElement).style.color = 'var(--error)')}
                        onMouseLeave={(e) => ((e.currentTarget as HTMLElement).style.color = 'var(--text-muted)')}
                      >
                        <Trash2 size={12} />
                      </button>
                    </li>
                  )
                })}
              </ul>
            )}
          </div>
        </>
      )}
    </div>
  )
}

function WorkflowValidationPanel({
  result, onClose, onHighlight, onAskAgent, onRevalidate, revalidating,
}: {
  result: WorkflowValidationResult
  onClose: () => void
  onHighlight: (nodeId: string | null | undefined) => void
  onAskAgent: () => void
  onRevalidate: () => void
  revalidating: boolean
}) {
  const errors = result.issues.filter((i) => i.severity === 'error')
  const warnings = result.issues.filter((i) => i.severity === 'warning')
  const infos = result.issues.filter((i) => i.severity === 'info')
  const ok = result.ok && result.issues.length === 0
  const headerColor = ok ? 'var(--success)' : errors.length ? 'var(--error)' : 'var(--warning)'
  const headerBg = ok ? 'var(--success-bg)' : errors.length ? 'var(--error-bg)' : 'var(--warning-bg)'

  return (
    <div className="panel mb-4" style={{ padding: 0, overflow: 'hidden', borderColor: headerColor }}>
      <div
        className="flex items-center justify-between px-4 py-2"
        style={{ background: headerBg, color: headerColor, fontWeight: 600 }}
      >
        <div className="flex items-center gap-2">
          {ok ? <CheckCircle2 size={14} /> : <AlertCircle size={14} />}
          <span style={{ fontSize: 13 }}>
            {ok
              ? 'Workflow looks good — no issues found.'
              : `Validation: ${errors.length} blocker${errors.length === 1 ? '' : 's'}` +
                (warnings.length ? `, ${warnings.length} warning${warnings.length === 1 ? '' : 's'}` : '') +
                (infos.length ? `, ${infos.length} note${infos.length === 1 ? '' : 's'}` : '')}
          </span>
          {!ok && (
            <span className="font-mono" style={{ fontSize: 11, opacity: 0.7, marginLeft: 4 }}>
              fix blockers before running
            </span>
          )}
        </div>
        <div className="flex items-center gap-1">
          <button
            onClick={onRevalidate}
            disabled={revalidating}
            className="text-[11px] font-semibold px-2 py-1 rounded-md disabled:opacity-50"
            style={{ color: headerColor }}
            title="Re-run validation"
          >
            {revalidating ? 'Checking…' : 'Re-check'}
          </button>
          <button onClick={onClose} className="p-1 rounded-md" style={{ color: headerColor }} title="Dismiss">
            <X size={14} />
          </button>
        </div>
      </div>

      {!ok && (
        <div style={{ borderTop: '1px solid var(--border-subtle)' }}>
          <ul className="divide-y" style={{ borderColor: 'var(--border-subtle)' }}>
            {[...errors, ...warnings, ...infos].map((issue, i) => (
              <ValidationIssueRow key={`${issue.code}-${i}`} issue={issue} onHighlight={onHighlight} />
            ))}
          </ul>
        </div>
      )}

      <div
        className="flex items-center justify-between px-4 py-2 text-xs"
        style={{ borderTop: '1px solid var(--border-subtle)', background: 'var(--bg-elevated)' }}
      >
        <span style={{ color: 'var(--text-muted)' }}>
          {ok
            ? "You're good to hit Run Workflow."
            : 'Click a row to highlight the node on the canvas.'}
        </span>
        <button
          onClick={onAskAgent}
          className="font-semibold px-2 py-1 rounded-md flex items-center gap-1"
          style={{ color: 'var(--accent)' }}
          title="Open the chat panel with these issues attached"
        >
          <Sparkles size={12} /> Ask the validator agent
          <ChevronRight size={12} />
        </button>
      </div>
    </div>
  )
}

function ValidationIssueRow({
  issue, onHighlight,
}: { issue: WorkflowValidationIssue; onHighlight: (id: string | null | undefined) => void }) {
  const tone = SEVERITY_TONE[issue.severity]
  const Icon = tone.icon
  return (
    <li
      className="px-4 py-2.5 text-xs flex items-start gap-3 cursor-pointer hover:bg-[var(--bg-elevated)]"
      onClick={() => onHighlight(issue.node_id)}
    >
      <span
        className="font-mono font-bold shrink-0 px-1.5 py-0.5 rounded"
        style={{ color: tone.color, background: tone.bg, fontSize: 10, letterSpacing: '0.05em' }}
      >
        {tone.label}
      </span>
      <div className="flex-1 min-w-0">
        <div style={{ color: 'var(--text-primary)', fontWeight: 500 }}>
          <Icon size={12} style={{ display: 'inline', verticalAlign: '-2px', marginRight: 4, color: tone.color }} />
          {issue.message}
        </div>
        {issue.hint && (
          <div className="mt-1" style={{ color: 'var(--text-secondary)', lineHeight: 1.55 }}>
            {issue.hint}
          </div>
        )}
      </div>
      {issue.node_id && (
        <span
          className="text-[10px] font-semibold shrink-0 self-center"
          style={{ color: 'var(--text-muted)' }}
        >
          ↗ highlight
        </span>
      )}
    </li>
  )
}

// ── Run-time error card ──────────────────────────────────────────────
function WorkflowRunErrorCard({
  detail, onClose, onHighlight, onAskAgent,
}: {
  detail: WorkflowRunErrorDetail
  onClose: () => void
  onHighlight: (id: string | null | undefined) => void
  onAskAgent: () => void
}) {
  const stepLabel = detail.step_index ? `Step ${detail.step_index}` : 'a step'
  const nodeLabel = detail.node_label || 'a model'
  return (
    <div
      className="panel mb-4"
      style={{ padding: 0, overflow: 'hidden', borderColor: 'var(--error)' }}
    >
      <div
        className="flex items-center justify-between px-4 py-2"
        style={{ background: 'var(--error-bg)', color: 'var(--error)', fontWeight: 600 }}
      >
        <div className="flex items-center gap-2">
          <AlertCircle size={14} />
          <span style={{ fontSize: 13 }}>
            Run failed at {stepLabel} · <span className="font-mono">{nodeLabel}</span>
          </span>
          <span
            className="font-mono px-1.5 py-0.5 rounded"
            style={{ fontSize: 10, background: 'var(--error)', color: '#fff', marginLeft: 4 }}
          >
            {detail.code}
          </span>
        </div>
        <button onClick={onClose} className="p-1 rounded-md" style={{ color: 'var(--error)' }} title="Dismiss">
          <X size={14} />
        </button>
      </div>

      <div className="px-4 py-3 space-y-3">
        <div>
          <div
            className="text-[10px] font-bold uppercase tracking-widest mb-1"
            style={{ color: 'var(--text-muted)' }}
          >
            What happened
          </div>
          <div
            className="text-xs font-mono leading-relaxed"
            style={{ color: 'var(--text-primary)' }}
          >
            {detail.what_happened}
          </div>
        </div>
        <div>
          <div
            className="text-[10px] font-bold uppercase tracking-widest mb-1"
            style={{ color: 'var(--text-muted)' }}
          >
            How to fix
          </div>
          <div className="text-xs leading-relaxed" style={{ color: 'var(--text-secondary)' }}>
            {detail.how_to_fix}
          </div>
        </div>
      </div>

      <div
        className="flex items-center justify-between px-4 py-2 text-xs"
        style={{ borderTop: '1px solid var(--border-subtle)', background: 'var(--bg-elevated)' }}
      >
        {detail.node_id ? (
          <button
            onClick={() => onHighlight(detail.node_id)}
            className="font-semibold flex items-center gap-1"
            style={{ color: 'var(--text-secondary)' }}
          >
            ↗ Highlight failed step on canvas
          </button>
        ) : (
          <span style={{ color: 'var(--text-muted)' }}>&nbsp;</span>
        )}
        <button
          onClick={onAskAgent}
          className="font-semibold px-2 py-1 rounded-md flex items-center gap-1"
          style={{ color: 'var(--accent)' }}
        >
          <Sparkles size={12} /> Ask the troubleshooter
          <ChevronRight size={12} />
        </button>
      </div>
    </div>
  )
}

// ── Result status strip ────────────────────────────────────────────────
function ResultStatusStrip({
  result, onClear,
}: { result: WorkflowResult; onClear: () => void }) {
  const ok = result.status === 'completed'
  const headerColor = ok ? 'var(--success)' : result.status === 'partial' ? 'var(--warning)' : 'var(--error)'
  const headerBg = ok ? 'var(--success-bg)' : result.status === 'partial' ? 'var(--warning-bg)' : 'var(--error-bg)'

  return (
    <div
      className="panel mb-4"
      style={{
        padding: 0,
        overflow: 'hidden',
        borderColor: headerColor,
      }}
    >
      <div
        className="flex items-center justify-between px-4 py-2"
        style={{ background: headerBg, color: headerColor, fontWeight: 600 }}
      >
        <div className="flex items-center gap-2">
          {ok ? <CheckCircle2 size={14} /> : <AlertCircle size={14} />}
          <span style={{ fontSize: 13 }}>
            Workflow {result.status === 'completed' ? 'completed' : result.status} ·{' '}
            <span className="font-mono">{result.workflow_id.slice(-8)}</span>
          </span>
          <span className="font-mono" style={{ fontSize: 11, opacity: 0.7, marginLeft: 4 }}>
            {result.runs.length} run{result.runs.length === 1 ? '' : 's'} ·{' '}
            {result.destinations.length} destination{result.destinations.length === 1 ? '' : 's'} ·{' '}
            {result.duration_ms.toFixed(0)}ms
          </span>
        </div>
        <button onClick={onClear} className="p-1 rounded-md" style={{ color: headerColor }}>
          <X size={14} />
        </button>
      </div>

      {result.error && (
        <div className="px-4 py-2 text-xs" style={{ color: 'var(--error)' }}>
          {result.error}
        </div>
      )}

      {result.destinations.length > 0 ? (
        <div style={{ borderTop: '1px solid var(--border-subtle)' }}>
          <div className="grid grid-cols-1 md:grid-cols-2 gap-0">
            {result.destinations.map((d) => (
              <DestinationStatusRow key={d.node_id} d={d} />
            ))}
          </div>
        </div>
      ) : (
        <div className="px-4 py-2 text-xs" style={{ color: 'var(--text-muted)' }}>
          No destinations connected. Drop a Snowflake / OneLake / S3 / CSV node and wire it to a model output.
        </div>
      )}
    </div>
  )
}

function DestinationStatusRow({ d }: { d: DestinationWrite }) {
  const meta = DESTINATION_META[d.kind]
  const Icon = meta?.icon || Download
  const ok = d.status === 'written'
  return (
    <div
      className="flex items-start gap-3 px-4 py-2.5"
      style={{ borderBottom: '1px solid var(--border-subtle)' }}
    >
      <div
        className="w-8 h-8 rounded-lg flex items-center justify-center shrink-0"
        style={{ background: `${meta?.color || '#0F766E'}1A`, color: meta?.color || '#0F766E' }}
      >
        <Icon size={14} />
      </div>
      <div className="min-w-0 flex-1">
        <div className="flex items-center gap-2">
          <span className="text-[12px] font-semibold" style={{ color: 'var(--text-primary)' }}>
            {meta?.label || d.kind}
          </span>
          <span
            className="pill"
            style={{
              fontSize: 9,
              background: ok ? 'var(--success-bg)' : 'var(--error-bg)',
              color: ok ? 'var(--success)' : 'var(--error)',
              borderColor: 'transparent',
            }}
          >
            {ok ? `WROTE ${d.rows_written} ROWS` : 'FAILED'}
          </span>
        </div>
        <div className="text-[11px] font-mono truncate mt-0.5" style={{ color: 'var(--text-secondary)' }}>
          {d.target}
        </div>
        {d.note && (
          <div className="text-[10px]" style={{ color: 'var(--text-muted)' }}>
            {d.note}
          </div>
        )}
      </div>
    </div>
  )
}

// ── Destination config modal ───────────────────────────────────────────
// ── Transform inspector (right-side drawer) ─────────────────────────────
function TransformInspector({
  node, transform, datasets, models,
  sourceNames, outputDatasetName,
  onSave, onRemove, onClose,
}: {
  node: RFNode<NodeData>
  transform: Transform
  datasets: Dataset[]
  models: TrainedModel[]
  sourceNames: string[]
  outputDatasetName: string
  onSave: (cfg: Record<string, any>, subtitle: string) => void
  onRemove: () => void
  onClose: () => void
}) {
  // Per-node config — selections live on this canvas instance, so the
  // user can drop two Data Harnesses with different configs.
  const initialDatasets: string[] = (node.data.config?.read_dataset_ids as string[]) || []
  const initialModels: string[] = (node.data.config?.requirement_model_ids as string[]) || []
  const [selectedDatasets, setSelectedDatasets] = useState<string[]>(initialDatasets)
  const [selectedModels, setSelectedModels] = useState<string[]>(initialModels)

  const toggleDataset = (id: string) => {
    setSelectedDatasets((prev) => prev.includes(id) ? prev.filter((x) => x !== id) : [...prev, id])
  }
  const toggleModel = (id: string) => {
    setSelectedModels((prev) => prev.includes(id) ? prev.filter((x) => x !== id) : [...prev, id])
  }

  // Auto-save on every change so the canvas immediately reflects the
  // user's selections (no separate Save button to remember).
  useEffect(() => {
    const subtitleBits: string[] = []
    if (selectedDatasets.length) subtitleBits.push(`${selectedDatasets.length} dataset${selectedDatasets.length === 1 ? '' : 's'}`)
    if (selectedModels.length)   subtitleBits.push(`${selectedModels.length} model req${selectedModels.length === 1 ? '' : 's'}`)
    const subtitle = subtitleBits.length
      ? subtitleBits.join(' · ')
      : (sourceNames.length ? `from ${sourceNames[0]}` : 'ETL recipe')
    onSave(
      {
        ...(node.data.config || {}),
        read_dataset_ids: selectedDatasets,
        requirement_model_ids: selectedModels,
      },
      subtitle,
    )
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [selectedDatasets, selectedModels])

  const confirmRemove = () => {
    if (window.confirm(`Remove "${transform.name}" from the canvas? This won't delete the transform itself — you can drag it back from the palette.`)) {
      onRemove()
    }
  }

  return (
    <>
      <div
        onClick={onClose}
        style={{
          position: 'fixed', inset: 0, background: 'rgba(0,0,0,0.35)', zIndex: 50,
        }}
      />
      <div
        className="panel"
        style={{
          position: 'fixed',
          top: 0, right: 0, bottom: 0,
          width: 'min(620px, 92vw)',
          padding: 0,
          borderRadius: 0,
          borderRight: 'none',
          borderTop: 'none',
          borderBottom: 'none',
          borderLeft: '1px solid var(--border)',
          zIndex: 60,
          display: 'flex', flexDirection: 'column',
        }}
      >
        <div
          className="px-4 py-3 flex items-center justify-between"
          style={{ borderBottom: '1px solid var(--border-subtle)', background: 'var(--bg-elevated)' }}
        >
          <div className="flex items-center gap-2">
            <div
              className="w-8 h-8 rounded-lg flex items-center justify-center"
              style={{ background: '#EA580C1A', color: '#EA580C' }}
            >
              <WorkflowIcon size={16} />
            </div>
            <div>
              <div className="text-sm font-bold" style={{ color: 'var(--text-primary)' }}>
                {transform.name}
              </div>
              <div className="text-[11px] font-mono" style={{ color: 'var(--text-muted)' }}>
                ETL transform · {transform.source}
              </div>
            </div>
          </div>
          <button onClick={onClose} className="p-1.5 rounded-md" title="Close">
            <X size={16} />
          </button>
        </div>

        <div className="flex-1 overflow-y-auto p-4 space-y-4">
          {transform.description && (
            <p className="text-xs leading-relaxed" style={{ color: 'var(--text-secondary)' }}>
              {transform.description}
            </p>
          )}

          {/* Lineage strip */}
          <div className="grid grid-cols-3 gap-2 text-[11px]">
            <LineageCell
              label="READS FROM"
              value={sourceNames.length ? sourceNames.join(' · ') : '(none)'}
              color="#0078D4"
            />
            <LineageCell label="" value="→" color="var(--text-muted)" centered />
            <LineageCell label="MATERIALIZES" value={outputDatasetName} color="#059669" />
          </div>

          {/* ── Read data from (datasets, multi-select) ─────────────── */}
          <MultiSelectGroup
            title="Read data from"
            sublabel="Datasets registered on the Data tab. Pick the ones this harness should pull rows from. Multiple selections are joined."
            empty="No datasets configured yet — add one on the Data tab."
            items={datasets.map((d) => ({
              id: d.id,
              title: d.name,
              hint: `${d.source_kind === 'upload' ? 'upload' : 'sql'} · ${d.columns.length} cols${d.row_count != null ? ` · ${d.row_count.toLocaleString()} rows` : ''}`,
            }))}
            selectedIds={selectedDatasets}
            onToggle={toggleDataset}
            color="#059669"
          />

          {/* ── Match requirements of (models, multi-select) ────────── */}
          <MultiSelectGroup
            title="Match requirements of"
            sublabel="Models registered on the Models tab. The harness ensures the rows it materializes carry every feature these models require."
            empty="No models configured yet — register one on the Models tab."
            items={models.map((m) => ({
              id: m.id,
              title: m.name,
              hint: `${m.model_type.toUpperCase()}${m.feature_columns?.length ? ` · ${m.feature_columns.length} features` : ''}`,
            }))}
            selectedIds={selectedModels}
            onToggle={toggleModel}
            color="#7C3AED"
          />

        </div>

        {/* Footer — Remove + close. Selecting a node + pressing
            Delete / Backspace also removes it; this button is the
            discoverable equivalent for users who didn't know that. */}
        <div
          className="px-4 py-3 flex items-center justify-between"
          style={{ borderTop: '1px solid var(--border-subtle)', background: 'var(--bg-elevated)' }}
        >
          <button
            onClick={confirmRemove}
            className="text-[11px] font-semibold px-2.5 py-1.5 rounded-md flex items-center gap-1.5"
            style={{ color: 'var(--error)', border: '1px solid var(--error)', background: 'transparent' }}
            title="Remove this node from the canvas"
          >
            <Trash2 size={12} /> Remove from canvas
          </button>
          <span className="text-[10px]" style={{ color: 'var(--text-muted)' }}>
            tip: select a node and press <span className="font-mono">Delete</span> to remove it
          </span>
        </div>
      </div>
    </>
  )
}

function MultiSelectGroup({
  title, sublabel, empty, items, selectedIds, onToggle, color,
}: {
  title: string
  sublabel: string
  empty: string
  items: { id: string; title: string; hint: string }[]
  selectedIds: string[]
  onToggle: (id: string) => void
  color: string
}) {
  const allSelected = items.length > 0 && selectedIds.length === items.length
  return (
    <div>
      <div className="flex items-baseline justify-between mb-1.5">
        <div
          className="text-[10px] font-bold uppercase tracking-widest"
          style={{ color: 'var(--text-muted)' }}
        >
          {title}
        </div>
        <div className="text-[10px] font-mono" style={{ color }}>
          {selectedIds.length} / {items.length} selected
        </div>
      </div>
      <div className="text-[11px] mb-2" style={{ color: 'var(--text-muted)', lineHeight: 1.55 }}>
        {sublabel}
      </div>
      {items.length === 0 ? (
        <div
          className="text-[11px] px-3 py-2 rounded-md"
          style={{ background: 'var(--bg-elevated)', color: 'var(--text-muted)', border: '1px dashed var(--border)' }}
        >
          {empty}
        </div>
      ) : (
        <div
          className="rounded-md divide-y"
          style={{ border: '1px solid var(--border-subtle)', borderColor: 'var(--border-subtle)' }}
        >
          <label
            className="px-3 py-1.5 flex items-center gap-2 text-[11px] font-semibold cursor-pointer"
            style={{ color: 'var(--text-secondary)', background: 'var(--bg-elevated)' }}
          >
            <input
              type="checkbox"
              checked={allSelected}
              onChange={() => {
                items.forEach((i) => {
                  const isSelected = selectedIds.includes(i.id)
                  if (allSelected && isSelected) onToggle(i.id)
                  if (!allSelected && !isSelected) onToggle(i.id)
                })
              }}
            />
            {allSelected ? 'Unselect all' : 'Select all'}
          </label>
          {items.map((it) => (
            <label
              key={it.id}
              className="px-3 py-2 flex items-center gap-2 cursor-pointer hover:bg-[var(--bg-elevated)]"
            >
              <input
                type="checkbox"
                checked={selectedIds.includes(it.id)}
                onChange={() => onToggle(it.id)}
              />
              <div className="min-w-0 flex-1">
                <div className="text-[12px] font-semibold truncate" style={{ color: 'var(--text-primary)' }}>
                  {it.title}
                </div>
                <div className="text-[10px] font-mono truncate" style={{ color: 'var(--text-muted)' }}>
                  {it.hint}
                </div>
              </div>
            </label>
          ))}
        </div>
      )}
    </div>
  )
}

function LineageCell({
  label, value, color, centered,
}: { label: string; value: string; color: string; centered?: boolean }) {
  return (
    <div
      className="rounded-md px-2 py-2"
      style={{
        background: 'var(--bg-elevated)',
        border: '1px solid var(--border-subtle)',
        textAlign: centered ? 'center' : 'left',
      }}
    >
      {label && (
        <div
          className="text-[9px] font-bold uppercase tracking-widest mb-1"
          style={{ color: 'var(--text-muted)' }}
        >
          {label}
        </div>
      )}
      <div
        className="text-[11px] font-semibold truncate"
        style={{ color }}
        title={value}
      >
        {value}
      </div>
    </div>
  )
}

function DestinationConfigModal({
  kind, initial, onClose, onSave,
}: {
  kind: DestinationKind
  initial: Record<string, any>
  onClose: () => void
  onSave: (config: Record<string, any>, subtitle: string) => void
}) {
  const meta = DESTINATION_META[kind]
  const [target, setTarget] = useState<string>(
    initial.table || initial.bucket || initial.filename || ''
  )
  const [mode, setMode] = useState<string>(initial.mode || 'append')
  const [keyPrefix, setKeyPrefix] = useState<string>(initial.key || '')
  const [format, setFormat] = useState<string>(initial.format || 'parquet')

  const submit = () => {
    if (!target.trim()) return
    let config: Record<string, any> = {}
    let subtitle = ''
    if (kind === 'snowflake_table') {
      config = { table: target.trim(), mode }
      subtitle = `${target.trim()} · ${mode}`
    } else if (kind === 'onelake_table') {
      config = { table: target.trim(), mode }
      subtitle = `${target.trim()} · ${mode}`
    } else if (kind === 's3') {
      config = { bucket: target.trim(), key: keyPrefix.trim() || undefined, format }
      subtitle = `s3://${target.trim()} · ${format}`
    } else if (kind === 'csv') {
      const fname = target.trim().endsWith('.csv') ? target.trim() : `${target.trim()}.csv`
      config = { filename: fname }
      subtitle = fname
    }
    onSave(config, subtitle)
  }

  return (
    <>
      <div className="fixed inset-0 z-40" style={{ background: 'rgba(11,15,25,0.45)' }} onClick={onClose} />
      <div
        className="fixed top-1/2 left-1/2 z-50 flex flex-col"
        style={{
          width: 'min(520px, 96vw)',
          transform: 'translate(-50%, -50%)',
          background: 'var(--bg-card)', border: '1px solid var(--border)',
          borderRadius: 12, boxShadow: '0 24px 64px rgba(0,0,0,0.20)',
        }}
      >
        <div
          className="flex items-center justify-between px-5 py-3.5"
          style={{
            background: `linear-gradient(135deg, ${meta.color}, ${meta.color}cc)`,
            borderTopLeftRadius: 11, borderTopRightRadius: 11,
          }}
        >
          <div className="flex items-center gap-2 font-display text-base font-semibold" style={{ color: '#fff' }}>
            <SettingsIcon size={14} /> Configure {meta.label}
          </div>
          <button onClick={onClose} className="p-1.5 rounded-lg" style={{ color: 'rgba(255,255,255,0.85)' }}>
            <X size={16} />
          </button>
        </div>

        <div className="px-5 py-4 space-y-4">
          <Field label={
            kind === 'snowflake_table' ? 'Table' :
            kind === 'onelake_table'   ? 'Table' :
            kind === 's3'              ? 'Bucket' :
                                         'Filename'
          }>
            <input
              autoFocus
              className="input font-mono"
              value={target}
              onChange={(e) => setTarget(e.target.value)}
              placeholder={meta.placeholder}
            />
            <div className="text-[11px] mt-1" style={{ color: 'var(--text-muted)' }}>
              {meta.configHint}
            </div>
          </Field>

          {(kind === 'snowflake_table' || kind === 'onelake_table') && (
            <Field label="Write Mode">
              <select className="input" value={mode} onChange={(e) => setMode(e.target.value)}>
                <option value="append">append (add rows)</option>
                <option value="overwrite">overwrite (replace table)</option>
                <option value="merge">merge (upsert by key)</option>
              </select>
            </Field>
          )}

          {kind === 's3' && (
            <>
              <Field label="Key prefix (optional)">
                <input
                  className="input font-mono"
                  value={keyPrefix}
                  onChange={(e) => setKeyPrefix(e.target.value)}
                  placeholder="analytics/{run_id}.parquet"
                />
              </Field>
              <Field label="Format">
                <select className="input" value={format} onChange={(e) => setFormat(e.target.value)}>
                  <option value="parquet">Parquet</option>
                  <option value="csv">CSV</option>
                  <option value="json">JSON Lines</option>
                </select>
              </Field>
            </>
          )}
        </div>

        <div className="flex items-center justify-end gap-2 px-5 py-3" style={{ borderTop: '1px solid var(--border)' }}>
          <button onClick={onClose} className="px-3 py-2 text-xs rounded-lg" style={{ color: 'var(--text-muted)' }}>
            Cancel
          </button>
          <button
            onClick={submit}
            disabled={!target.trim()}
            className="px-3 py-2 rounded-lg text-xs font-semibold disabled:opacity-40"
            style={{ background: meta.color, color: '#fff' }}
          >
            Save Destination
          </button>
        </div>
      </div>
      <style>{`
        .input {
          width: 100%; padding: 8px 10px; border-radius: 8px; font-size: 13px;
          background: var(--bg-elevated); border: 1px solid var(--border); color: var(--text-primary);
        }
      `}</style>
    </>
  )
}
