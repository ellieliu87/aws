import { create } from 'zustand'
import { persist, createJSONStorage } from 'zustand/middleware'
import type { ChatMessageItem } from '@/types'

export type EntityKind =
  | 'kpi' | 'dataset' | 'scenario' | 'model' | 'run' | 'tile' | 'workflow' | 'analytic_def' | 'transform' | 'document' | null
export type TabId =
  | 'overview' | 'data' | 'models' | 'workflow' | 'analytics' | 'reporting' | 'playbooks' | 'settings' | null

interface ChatState {
  messages: ChatMessageItem[]
  isLoading: boolean
  isOpen: boolean
  pageContext: string | null
  functionId: string | null
  tab: TabId
  entityKind: EntityKind
  entityId: string | null
  payload: Record<string, any> | null
  addMessage: (m: Omit<ChatMessageItem, 'id'>) => void
  clearMessages: () => void
  setLoading: (b: boolean) => void
  setOpen: (b: boolean) => void
  setPageContext: (s: string | null) => void
  setFunctionId: (s: string | null) => void
  setTab: (t: TabId) => void
  setEntity: (kind: EntityKind, id: string | null) => void
  setPayload: (p: Record<string, any> | null) => void
}

export const useChatStore = create<ChatState>()(
  persist(
    (set) => ({
      messages: [],
      isLoading: false,
      isOpen: false,
      pageContext: null,
      functionId: null,
      tab: null,
      entityKind: null,
      entityId: null,
      payload: null,
      addMessage: (m) =>
        set((s) => ({
          messages: [...s.messages, { ...m, id: `msg-${Date.now()}-${Math.random().toString(36).slice(2, 7)}` }],
        })),
      clearMessages: () => set({ messages: [] }),
      setLoading: (b) => set({ isLoading: b }),
      setOpen: (b) => set({ isOpen: b }),
      setPageContext: (s) => set({ pageContext: s }),
      setFunctionId: (s) => set({ functionId: s }),
      setTab: (t) => set({ tab: t }),
      setEntity: (kind, id) => set({ entityKind: kind, entityId: id }),
      setPayload: (p) => set({ payload: p }),
    }),
    {
      name: 'cma-chat',
      storage: createJSONStorage(() => localStorage),
      // Only persist the conversation thread. Page-specific state (open
      // flag, current tab/entity/context, in-flight loading) should reset
      // each session — re-opening the panel on a different tab shouldn't
      // resurface stale entity context.
      partialize: (state) => ({ messages: state.messages }),
    },
  ),
)
