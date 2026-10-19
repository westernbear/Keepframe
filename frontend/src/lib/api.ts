import { errorText } from '@/i18n'

const ADMIN_LOGIN_PATH = '/admin/login'

export class ApiError extends Error {
  status: number
  body: any
  constructor(message: string, status: number, body: any) {
    super(message)
    this.status = status
    this.body = body
  }
}

export function withQuery(url: string, params: Record<string, unknown>) {
  const q = new URLSearchParams()
  Object.entries(params).forEach(([key, value]) => {
    if (value == null || value === '') return
    q.set(key, String(value))
  })
  const encoded = q.toString()
  return encoded ? `${url}?${encoded}` : url
}

async function readJsonOrNull(res: Response) {
  const contentType = res.headers.get('content-type') || ''
  if (!contentType.includes('json')) return null
  return res.json().catch(() => ({}))
}

export async function api(path: string, opts: RequestInit = {}): Promise<any> {
  const isForm = opts.body instanceof FormData
  const res = await fetch(path, {
    headers: isForm ? { ...(opts.headers || {}) } : { 'Content-Type': 'application/json', ...(opts.headers || {}) },
    ...opts,
  })
  const data = await readJsonOrNull(res)
  if (!res.ok) throw new ApiError(errorText(data && data.error) || res.statusText, res.status, data)
  return data
}

export async function fetchAdminOrRedirect(path: string, opts: RequestInit = {}) {
  try {
    return await api(path, opts)
  } catch (err) {
    if ((err as ApiError).status === 401) location.assign(ADMIN_LOGIN_PATH)
    throw err
  }
}

const post = (path: string, body: unknown) => api(path, { method: 'POST', body: JSON.stringify(body) })
const enc = encodeURIComponent

export const fetchProjects = () => api('/api/projects')
export const fetchProject = (projectId: string) => api(`/api/projects/${enc(projectId)}`)
export const fetchStatus = () => api('/api/status')
export const fetchJob = (jobId: string) => api(`/api/jobs/${enc(jobId)}`)
export const cancelJob = (jobId: string) => api(`/api/jobs/${enc(jobId)}/cancel`, { method: 'POST' })
export const deleteProject = (projectId: string) => api(`/api/projects/${enc(projectId)}`, { method: 'DELETE' })
export const postAnalyze = (body: unknown) => post('/api/analyze', body)
export const postAdminLogin = ({ email, password }: { email: string; password: string }) =>
  post('/admin/api/login', { email, password })

export function uploadProject({ title, video, mode, start, end }: { title: string; video: File; mode: string; start: number; end: number }) {
  const fd = new FormData()
  fd.append('title', title)
  fd.append('video', video)
  fd.append('mode', mode)
  fd.append('start', String(start))
  fd.append('end', String(end))
  return api('/api/projects', { method: 'POST', body: fd })
}

export const fetchEstimate = (body: unknown) => post('/api/estimate', body)
export const DEFAULT_FILMSTRIP_COUNT = 8
export const fetchFilmstrip = (projectId: string, n = DEFAULT_FILMSTRIP_COUNT) => api(`/api/projects/${projectId}/filmstrip?n=${n}`)
export const fetchFonts = (projectId: string) => api(`/api/projects/${enc(projectId)}/fonts`)
export function uploadFont(projectId: string, file: File) {
  const path = withQuery(`/api/projects/${enc(projectId)}/fonts`, { name: file.name })
  return api(path, { method: 'POST', headers: { 'Content-Type': 'application/octet-stream' }, body: file })
}

export const fetchReviewState = (project: string, scene: string, v?: string | null) => api(withQuery('/api/state', { project, scene, v }))
export const fetchReviewJob = (project: string, scene: string) => api(withQuery('/api/job', { project, scene }))
export const fetchAnalysisOverlay = (project: string, scene: string, frame: number, v: string | null | undefined, signal?: AbortSignal) =>
  api(withQuery('/api/analysis-overlay', { project, scene, frame, v }), { signal })
export const fetchBboxes = (project: string, scene: string, frame: number, v?: string | null) =>
  api(withQuery('/api/bboxes', { project, scene, frame, v }))

export function postApprove(project: string, scene: string, v?: string | null) {
  const body: Record<string, unknown> = { project, scene }
  if (v) body.v = v
  return post('/api/approve', body)
}

export const postKeep = (project: string, scene: string, changes: unknown, note: unknown, preset: unknown) =>
  post('/api/keep', { project, scene, changes, note, preset })
export const postEdit = (body: unknown) => post('/api/edit', body)
export const postAgent = (body: unknown) => post('/api/agent', body)
export const fetchAgentHistory = (project: string, scene: string, before?: unknown, limit = 50) =>
  api(withQuery('/api/agent', { project, scene, before, limit }))
export const fetchRenderPlans = (project: string, scene: string, version?: string | null) =>
  api(withQuery('/api/render-plans', { project, scene, version }))
export const fetchRenderState = (project: string, plan: string) => api(withQuery('/api/render-state', { project, plan }))
export const postRenderPlan = (body: unknown) => post('/api/render-plans', body)
export const approveRenderPlan = (planId: string, body: unknown) => post(`/api/render-plans/${enc(planId)}/approve`, body)
export const nativeArtifactUrl = (planId: string, kind: string, project: string) =>
  withQuery(`/api/native/artifacts/${enc(planId)}/${enc(kind)}`, { project })
export const lottieArtifactUrl = (planId: string, project: string) => withQuery(`/api/lottie/artifacts/${enc(planId)}/animation`, { project })
export const postCorrect = (project: string, scene: string, op: string, args: unknown) => post('/api/correct', { project, scene, op, args })
export const reviewFrameUrl = (kind: string, frame: number, project: string, scene: string, v?: string | null) =>
  withQuery(`/frame/${kind}/${frame}`, { project, scene, v })
export const reviewAssetUrl = (name: string, project: string, scene: string) =>
  `/assets/${enc(name)}?project=${enc(project)}&scene=${enc(scene)}`
