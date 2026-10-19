import { describe, expect, it } from 'vitest'
import { T, errorText } from '@/i18n'
import { badgeClass, badgeText, formatConfidence, formatVersion, hrefForProject, statusLabel, thumbUrl, visibleProjects } from './library'

describe('library', () => {
  // Ported from tests/test_web_static.py::test_landing_and_library_link_to_demo (the library href rules)
  it('links each project by status: ingest, analyze, review, agent', () => {
    expect(hrefForProject({ id: 'a b', status: 'uploaded' })).toBe('/new?project=a%20b')
    expect(hrefForProject({ id: 'p', status: 'analyzing', job_id: 'j' })).toBe('/analyze?job=p')
    expect(hrefForProject({ id: 'p', status: 'analyzing' })).toBe('/review?project=p&scene=s1')
    expect(hrefForProject({ id: 'p', status: 'review', scene: 's2' })).toBe('/review?project=p&scene=s2')
    expect(hrefForProject({ id: 'p', status: 'approved', scene: 's2', version: 'v3' })).toBe('/agent?project=p&scene=s2&v=v3')
    expect(hrefForProject({ id: 'p', status: 'approved' })).toBe('/agent?project=p&scene=s1')
    expect(hrefForProject({ id: 'p', status: 'error' })).toBeNull()
    expect(hrefForProject({ id: 'p', status: 'rejected' })).toBeNull()
  })

  // Ported from tests/test_web_static.py::test_library_filters_are_wired
  it('filters by the review chip and by title or id', () => {
    const projects = [{ id: 'p1', title: 'Launch Promo', status: 'review' }, { id: 'p2', status: 'approved' }, { id: 'x3', title: 'Promo B', status: 'approved' }]
    expect(visibleProjects(projects, 'all', '')).toEqual(projects)
    expect(visibleProjects(projects, 'review', '')).toEqual([projects[0]])
    expect(visibleProjects(projects, 'all', '  PROMO ')).toEqual([projects[0], projects[2]])
    expect(visibleProjects(projects, 'all', 'p2')).toEqual([projects[1]])
    expect(visibleProjects(projects, 'review', 'p2')).toEqual([])
  })

  it('badges, versions and confidence', () => {
    expect(statusLabel('review')).toBe(T('status.review'))
    expect(statusLabel('mystery')).toBe('mystery')
    expect(badgeText({ status: 'rejected', reason: 'live action' })).toBe('live action')
    expect(badgeText({ status: 'rejected' })).toBe(T('status.rejected'))
    expect(badgeText({ status: 'error', error: 'job_failed' })).toBe(errorText('job_failed'))
    expect(badgeText({ status: 'error' })).toBe(T('status.error'))
    expect(badgeClass('analyzing')).toBe('badge--active')
    expect(badgeClass('rejected')).toBe('badge--error')
    expect(badgeClass('mystery')).toBe('badge--done')
    expect(formatVersion(3)).toBe('v3')
    expect(formatVersion('v3')).toBe('v3')
    expect(formatVersion('')).toBeNull()
    expect(formatConfidence(0.8)).toBe('0.80')
    expect(formatConfidence('high')).toBe('high')
    expect(formatConfidence(null)).toBeNull()
    expect(thumbUrl({ id: 'a/b', status: 'review' })).toBe('/api/projects/a%2Fb/frame/0')
    expect(thumbUrl({ id: 'p', status: 'rejected' })).toBeNull()
  })
})
