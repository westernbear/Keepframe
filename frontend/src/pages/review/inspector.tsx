import { memo, useState } from 'react'
import { Button } from '@/components/app/button'
import Badge from '@/components/smoothui/badge'
import Checkbox from '@/components/smoothui/checkbox'
import { TabsIndicator, TabsList, TabsPanel, TabsRoot, TabsTab } from '@/components/smoothui/tabs'
import { useI18n } from '@/i18n'
import { confirmAction } from '@/components/app/confirm'
import { KEEP_PRESETS, bboxArgs, constraintChecked, keepLabel, maskArgs, presetDiff, reassignArgs, textArgs } from '@/lib/corrections'
import { readFileAsBase64 } from '@/lib/files'
import { elementLabel, fontCorrection, fontPicker, isLatestVersion, matchesElementFilter, objectDetail } from '@/lib/inspector'
import { CONSTRAINT_STEP } from '@/lib/review-geom'
import { cn } from '@/lib/utils'
import { objectColor } from './colors'
import type { ReviewCore, ReviewView, Tool } from './core'

const field = 'h-10 w-full min-w-0 rounded-lg border border-hairline bg-canvas px-3 text-body-sm outline-none focus-visible:border-brand disabled:opacity-50'
const labelCls = 'flex flex-col gap-1.5 text-caption text-ink-muted'
const TOOLS: [Tool, string][] = [['bbox', 'review.bbox'], ['edit', 'review.edit'], ['text', 'review.text'], ['reassign', 'review.reassign'], ['mask', 'review.mask']]

const ElementList = memo(function ElementList({ elements, selectedId, filter, core }: { elements: any[]; selectedId: string | null; filter: string; core: ReviewCore }) {
  const { T } = useI18n()
  return (
    <div id="element-list" className="flex flex-col gap-0.5">
      {elements.map((item) => {
        const label = elementLabel(item)
        const selected = item.id === selectedId
        return (
          <button key={item.id} type="button" data-id={item.id} aria-pressed={selected} hidden={!matchesElementFilter(filter, item.id, label)}
            onClick={() => core.selectElement(item.id)}
            className={cn('element-row state-transition flex min-h-10 w-full items-center gap-2.5 rounded-lg px-2.5 text-left text-body-sm', selected ? 'element-row--selected bg-white/[0.07] text-ink' : 'text-ink-muted hover:bg-white/[0.04] hover:text-ink')}>
            <span className="object-swatch size-2.5 shrink-0 rounded-full" style={{ background: objectColor(item.id) }} />
            <span className="element-row__meta min-w-0 truncate"><span className="font-mono text-ink">{item.id}</span> · {T(`review.kind.${item.kind}`)}{item.canonical?.text ? ` · ${item.canonical.text}` : ''}</span>
          </button>
        )
      })}
    </div>
  )
})

function ObjectDetail({ view, core }: { view: ReviewView; core: ReviewCore }) {
  const { T } = useI18n()
  const st = view.st
  const item = st?.scene.elements.find((e: any) => e.id === view.selectedId)
  if (!item) return <section id="object-detail" className="object-detail" aria-live="polite" />
  const d = objectDetail(item, view.overlay?.objects.find((o: any) => o.id === item.id), st.analysis?.objects.find((o: any) => o.id === item.id), st.analysis, view.versionId, st.project.versions)
  return (
    <section id="object-detail" className="object-detail flex flex-col gap-3 rounded-xl bg-surface-2/60 p-3.5" aria-live="polite">
      <h2 className="m-0 text-body-lg font-semibold">{d.title}</h2>
      <dl className="m-0 grid grid-cols-[auto_1fr] gap-x-4 gap-y-1.5 text-caption">
        {d.lines.map(([k, v]) => (
          <div key={k} className="contents"><dt className="text-ink-muted">{T(k)}</dt><dd className="m-0 min-w-0 break-words tabular-nums">{v}</dd></div>
        ))}
      </dl>
      {d.pending3d && (
        <div className="flex flex-wrap items-center gap-2">
          <Badge variant="outline" className="badge badge--active border-brand/40 text-brand-soft" data-i18n="review.pending3d">{T('review.pending3d')}</Badge>
          <Button tone="secondary" size="sm" data-i18n="review.generate3d" disabled={d.pending3d.disabled} title={d.pending3d.title} onClick={() => core.previewReference3d(item.id)}>{T('review.generate3d')}</Button>
        </div>
      )}
    </section>
  )
}

function FontCandidates({ item, core }: { item: any; core: ReviewCore }) {
  const { T } = useI18n()
  const picker = fontPicker(item)
  const [family, setFamily] = useState(picker?.value || '')
  if (!picker) return <div id="font-candidates" />
  return (
    <div id="font-candidates" className="flex flex-col gap-2">
      <label className={labelCls} htmlFor="font-candidate-family" data-i18n="review.fontCandidates">{T('review.fontCandidates')}</label>
      <div className="flex gap-2">
        <select id="font-candidate-family" className={field} value={family} onChange={(e) => setFamily(e.target.value)}>
          {picker.families.map((f: string) => <option key={f} value={f}>{f}</option>)}
        </select>
        <Button tone="secondary" size="sm" className="h-10 shrink-0" data-i18n="review.applyFont" onClick={() => core.runCorrect('text', fontCorrection(item.id, picker.font, family))}>{T('review.applyFont')}</Button>
      </div>
    </div>
  )
}

function EditPanel({ view, core }: { view: ReviewView; core: ReviewCore }) {
  const { T } = useI18n()
  const e = view.edit
  return (
    <div className="flex flex-col gap-3" id="acc-edit">
      <label className={labelCls}>{T('review.editPrompt')}
        <input id="edit-prompt" className={field} type="text" value={e.prompt} onChange={(ev) => core.setEdit({ prompt: ev.target.value })} />
      </label>
      <label className={labelCls}>{T('review.editAttach')}
        <input id="edit-file" type="file" accept="image/png,image/jpeg,image/webp" className="text-caption text-ink-muted file:mr-3 file:rounded-sm file:border-0 file:bg-surface-3 file:px-3 file:py-1.5 file:text-ink"
          onChange={(ev) => core.setEdit({ file: ev.target.files?.[0] || null })} />
      </label>
      {e.summary && <p className="m-0 rounded-lg bg-surface-2 p-3 text-body-sm" id="edit-summary">{e.summary}</p>}
      {(e.conflicts.length > 0 || e.needElement) && (
        <div id="edit-conflicts" className="flex flex-col gap-3">
          {e.needElement && <p className="m-0 text-body-sm text-warning">{T('review.editNeedElement')}</p>}
          {e.conflicts.map((c: any) => (
            <fieldset key={c.id} className="m-0 flex flex-col gap-1.5 border-0 p-0">
              <legend className="mb-1 text-caption text-ink-muted">{c.reason || c.element}</legend>
              {(c.choices || []).map((ch: string) => (
                <label key={ch} className="constraint-row inline-flex min-h-9 items-center gap-2 text-body-sm">
                  <input type="radio" name={`edit-${c.id}`} value={ch} checked={e.choices[c.id] === ch}
                    onChange={() => core.setEdit({ choices: { ...e.choices, [c.id]: ch } })} className="accent-[var(--brand)]" />
                  {T(`review.editChoice.${ch}`)}
                </label>
              ))}
            </fieldset>
          ))}
        </div>
      )}
      <div className="flex flex-wrap gap-2">
        <Button tone="secondary" size="sm" id="edit-run" data-i18n="review.editRun" loading={e.busy && !e.needsConfirm} onClick={() => core.runEdit(e, false)}>{T('review.editRun')}</Button>
        {e.needsConfirm && <>
          <Button tone="primary" size="sm" id="edit-confirm" loading={e.busy} onClick={() => core.runEdit(e, true)}>{T('review.editConfirm')}</Button>
          <Button tone="ghost" size="sm" id="edit-cancel" onClick={core.resetEdit}>{T('review.editCancel')}</Button>
        </>}
      </div>
    </div>
  )
}

export function Inspector({ view, core }: { view: ReviewView; core: ReviewCore }) {
  const { T, Tf } = useI18n()
  const [constraintLimit, setConstraintLimit] = useState(CONSTRAINT_STEP)
  const st = view.st
  const elements = st?.scene.elements || []
  const item = elements.find((e: any) => e.id === view.selectedId)
  const latest = st && isLatestVersion(view.versionId, st.project.versions)
  const formsDisabled = view.jobRunning || !latest
  const f = view.forms
  const setForm = (p: Partial<ReviewView['forms']>) => core.patchForms(p)
  const constraints = st?.scene.constraints || []
  const keepDirty = view.keepPending.size > 0

  return (
    <aside className="inspector flex min-w-0 flex-col gap-4 [&>*]:shrink-0 lg:max-h-[calc(100dvh-8rem)] lg:overflow-y-auto lg:pr-1">
      <div className="inspector__head flex items-center justify-between">
        <span id="element-count" className="text-body-sm font-semibold">{Tf('review.elements', { n: elements.length })}</span>
      </div>
      <p id="job-banner" role="status" hidden={!view.banner}
        className={cn('job-banner m-0 rounded-xl px-3.5 py-2.5 text-body-sm', view.banner?.error ? 'job-banner--error bg-danger/10 text-danger' : 'bg-brand/10 text-brand-soft')}>
        {view.banner?.msg}
      </p>
      {st && !elements.length && <div id="empty-state" className="empty-state rounded-xl bg-surface-1 p-4 text-body-sm text-ink-muted">{T('review.empty')}</div>}
      <input id="element-filter" type="search" className={cn('element-filter', field, 'rounded-md bg-surface-1')} placeholder={T('review.filter')} aria-label={T('review.filter')}
        hidden={!elements.length} value={view.filter} onChange={(e) => core.patch({ filter: e.target.value })} />
      <ElementList elements={elements} selectedId={view.selectedId} filter={view.filter} core={core} />
      <ObjectDetail view={view} core={core} />

      {st && (
        <section className="review-constraints flex flex-col gap-3 rounded-xl bg-surface-1 p-3.5 ring-1 ring-inset ring-hairline" aria-labelledby="keep-title">
          <div className="flex flex-col gap-0.5">
            <h3 id="keep-title" className="m-0 text-body-sm font-medium">{T('review.keep')}</h3>
            <p className="m-0 text-caption text-ink-muted">{!constraints.length ? T('review.keepNone') : Tf('review.keepHint', { n: constraints.filter((c: any) => constraintChecked(c, view.keepPending)).length, total: constraints.length })}</p>
          </div>
          <div id="constraints-panel" className="flex flex-col gap-3" hidden={!constraints.length}>
            <div className="keep-presets flex flex-wrap gap-1.5">
              {KEEP_PRESETS.map((p) => (
                <Button key={p} tone="secondary" size="xs" disabled={view.jobRunning} onClick={async () => {
                  const { on, off } = presetDiff(p, constraints, view.keepPending)
                  const ok = await confirmAction({
                    title: Tf('review.keepPresetConfirm', { preset: T(`review.keepPreset.${p}`) }),
                    description: on || off ? Tf('review.keepPresetDiff', { on, off }) : T('review.keepPresetSame'),
                    confirmLabel: T('review.keepPresetApply'),
                  })
                  if (ok) core.saveKeep(p)
                }}>{T(`review.keepPreset.${p}`)}</Button>
              ))}
            </div>
            <div className="flex flex-col">
              {constraints.slice(0, constraintLimit).map((c: any) => {
                const label = keepLabel(c.pred)
                return (
                  <label key={c.pred} className="constraint-row flex min-h-10 items-center gap-2.5 text-caption" title={c.pred}>
                    <Checkbox checked={constraintChecked(c, view.keepPending)} onCheckedChange={(on) => core.setKeep(c.pred, on)} />
                    <span className="min-w-0 break-words">
                      {label ? Tf(label.key, label.vars) : <span className="break-all font-mono">{c.pred}</span>}
                    </span>
                  </label>
                )
              })}
            </div>
            {constraints.length > constraintLimit && (
              <Button tone="ghost" size="sm" className="btn--more-constraints self-start" onClick={() => setConstraintLimit((n) => n + CONSTRAINT_STEP)}>
                {Tf('review.moreConstraints', { n: constraints.length - constraintLimit })}
              </Button>
            )}
          </div>
        </section>
      )}
      <Button tone="primary" id="keep-save" hidden={!keepDirty} disabled={!keepDirty || view.jobRunning} onClick={() => core.saveKeep()}>{T('review.keepSave')}</Button>

      {item && fontPicker(item) && (
        <fieldset className="m-0 min-w-0 rounded-xl border-0 bg-surface-1 p-3.5 ring-1 ring-inset ring-hairline" disabled={formsDisabled}>
          <FontCandidates key={`${item.id}-${view.versionId}`} item={item} core={core} />
        </fieldset>
      )}
      {!(item && fontPicker(item)) && <div id="font-candidates" hidden />}

      {st && (
        <TabsRoot value={view.tool} onValueChange={(v) => core.patch({ tool: v as Tool })} className="rounded-xl bg-surface-1 ring-1 ring-inset ring-hairline">
          <TabsList variant="underline" className="overflow-x-auto px-1 [scrollbar-width:none]">
            {TOOLS.map(([k, label]) => <TabsTab key={k} value={k} className="px-2.5 py-2.5 text-caption">{T(label)}</TabsTab>)}
            <TabsIndicator />
          </TabsList>
          <div className="p-3.5">
            <TabsPanel value="edit" className="pt-0 text-ink"><EditPanel view={view} core={core} /></TabsPanel>
            <fieldset id="forms-panel" className="inspector__forms m-0 min-w-0 border-0 p-0" disabled={formsDisabled}>
              <TabsPanel value="bbox" className="flex flex-col gap-3 pt-0 text-ink" id="acc-bbox">
                <p className="empty-state m-0 text-caption text-ink-muted">{T('review.drawHint')}</p>
                <div className="grid grid-cols-[88px_1fr] gap-2">
                  <label className={labelCls}>{T('review.frame')}<input id="bbox-frame" className={field} type="number" value={f.bboxFrame} onChange={(e) => setForm({ bboxFrame: e.target.value })} /></label>
                  <label className={labelCls}>{T('review.bboxLabel')}<input id="bbox-coords" className={cn(field, 'font-mono')} type="text" placeholder="x0,y0,x1,y1" value={f.bboxCoords} onChange={(e) => setForm({ bboxCoords: e.target.value })} /></label>
                </div>
                <Button tone="secondary" size="sm" className="self-start" id="bbox-run" onClick={() => core.runCorrect('bbox', bboxArgs(f.bboxCoords, f.bboxFrame, view.selectedId))}>{T('review.runBbox')}</Button>
              </TabsPanel>
              <TabsPanel value="text" className="flex flex-col gap-3 pt-0 text-ink" id="acc-text">
                <label className={labelCls}>text<input id="text-value" className={field} type="text" value={f.textValue} onChange={(e) => setForm({ textValue: e.target.value })} /></label>
                <Button tone="secondary" size="sm" className="self-start" id="text-run" disabled={item?.kind !== 'text'} onClick={() => core.runCorrect('text', textArgs(view.selectedId, f.textValue))}>{T('review.runText')}</Button>
              </TabsPanel>
              <TabsPanel value="reassign" className="flex flex-col gap-3 pt-0 text-ink" id="acc-reassign">
                <label className={labelCls}>{T('review.from')}<input id="reassign-from" className={cn(field, 'font-mono')} type="text" value={f.reassignFrom} onChange={(e) => setForm({ reassignFrom: e.target.value })} /></label>
                <label className={labelCls}>{T('review.to')}
                  <select id="reassign-to" className={cn(field, 'font-mono')} value={f.reassignTo} onChange={(e) => setForm({ reassignTo: e.target.value })}>
                    {elements.map((e: any) => <option key={e.id} value={e.id}>{e.id}</option>)}
                  </select>
                </label>
                <label className={labelCls}>{T('review.frames')}<input id="reassign-frames" className={cn(field, 'font-mono')} type="text" placeholder="0,59" value={f.reassignFrames} onChange={(e) => setForm({ reassignFrames: e.target.value })} /></label>
                <Button tone="secondary" size="sm" className="self-start" id="reassign-run" onClick={() => core.runCorrect('reassign', reassignArgs(f.reassignFrom, f.reassignTo, f.reassignFrames, st.scene.frames))}>{T('review.runReassign')}</Button>
              </TabsPanel>
              <TabsPanel value="mask" className="flex flex-col gap-3 pt-0 text-ink" id="acc-mask">
                <label className={labelCls}>{T('review.png')}
                  <input id="mask-file" type="file" accept="image/png" className="text-caption text-ink-muted file:mr-3 file:rounded-sm file:border-0 file:bg-surface-3 file:px-3 file:py-1.5 file:text-ink"
                    onChange={(e) => setForm({ maskFile: e.target.files?.[0] || null })} />
                </label>
                <label className={labelCls}>{T('review.frame')}<input id="mask-frame" className={field} type="number" value={f.maskFrame} onChange={(e) => setForm({ maskFrame: e.target.value })} /></label>
                <Button tone="secondary" size="sm" className="self-start" id="mask-run" disabled={item?.kind === 'text'}
                  onClick={async () => { if (f.maskFile) core.runCorrect('mask', maskArgs(f.maskFrame, await readFileAsBase64(f.maskFile), view.selectedId)) }}>{T('review.runMask')}</Button>
              </TabsPanel>
            </fieldset>
          </div>
        </TabsRoot>
      )}
    </aside>
  )
}
