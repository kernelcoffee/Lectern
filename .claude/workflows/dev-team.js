// Lectern dev-team workflow.
//
// Invoke from the orchestrator (Fable) with a task:
//   Workflow({ name: "dev-team", args: { task: "<task or full brief>", rounds: 2 } })
// or in plain words: "run the dev-team workflow on: <task>".
//
// Shape: plan (Fable) -> implement -> three reviewers in parallel -> one
// refuter per finding -> implementer fixes survivors (up to `rounds`) ->
// docs writer -> QA -> one repair round.
//
// The planner stops the run when it has questions only the user can answer;
// resume with args.decisions (free text answering them) or args.proceed=true
// to accept its recommendations. Pass args.plan=false to skip planning when
// the task already is a vetted brief.
//
// Every worker runs on Opus; the planner and the orchestrator run on Fable.

export const meta = {
  name: 'dev-team',
  description: 'Plan, implement, review from three angles, refute findings, fix survivors, update docs, run QA',
  whenToUse: 'Code changes on Lectern that should go through plan, implement, review, docs and QA without the orchestrator doing the work itself',
  phases: [
    { title: 'Plan', detail: 'planner turns the task into a brief; stops on open decisions' },
    { title: 'Implement', detail: 'implementer builds the change' },
    { title: 'Review', detail: 'correctness, security, conventions reviewers in parallel' },
    { title: 'Verify', detail: 'one independent refuter per finding' },
    { title: 'Fix', detail: 'implementer addresses confirmed findings' },
    { title: 'Docs', detail: 'docs writer aligns functional, technical and README' },
    { title: 'QA', detail: 'full suite, then one repair round if red' },
  ],
}

const task = typeof args === 'string' ? args : args && args.task
if (!task) throw new Error('Pass the task as args (a string) or as args.task')
const opt = (typeof args === 'object' && args) || {}
const maxRounds = opt.rounds || 2
const MODEL = 'opus'
const PLANNER_MODEL = 'fable'

const PLAN = {
  type: 'object',
  properties: {
    summary: { type: 'string' },
    brief: { type: 'string' },
    files: { type: 'array', items: { type: 'string' } },
    risks: { type: 'array', items: { type: 'string' } },
    assumptions: { type: 'array', items: { type: 'string' } },
    needs_decision: { type: 'array', items: { type: 'string' } },
  },
  required: ['summary', 'brief', 'needs_decision'],
}

const IMPL = {
  type: 'object',
  properties: {
    summary: { type: 'string' },
    files_changed: { type: 'array', items: { type: 'string' } },
    tests_added: { type: 'array', items: { type: 'string' } },
    checks_passed: { type: 'boolean' },
    open_questions: { type: 'array', items: { type: 'string' } },
  },
  required: ['summary', 'files_changed', 'checks_passed'],
}

const FINDINGS = {
  type: 'object',
  properties: {
    findings: {
      type: 'array',
      items: {
        type: 'object',
        properties: {
          file: { type: 'string' },
          line: { type: 'integer' },
          severity: { type: 'string', enum: ['high', 'medium', 'low'] },
          summary: { type: 'string' },
          failure_scenario: { type: 'string' },
          suggested_fix: { type: 'string' },
        },
        required: ['file', 'severity', 'summary', 'failure_scenario'],
      },
    },
  },
  required: ['findings'],
}

const VERDICT = {
  type: 'object',
  properties: {
    refuted: { type: 'boolean' },
    reason: { type: 'string' },
  },
  required: ['refuted', 'reason'],
}

const DOCS = {
  type: 'object',
  properties: {
    summary: { type: 'string' },
    files_changed: { type: 'array', items: { type: 'string' } },
    gaps: { type: 'array', items: { type: 'string' } },
  },
  required: ['summary', 'files_changed'],
}

const QA = {
  type: 'object',
  properties: {
    all_green: { type: 'boolean' },
    suites: {
      type: 'array',
      items: {
        type: 'object',
        properties: {
          name: { type: 'string' },
          passed: { type: 'boolean' },
          detail: { type: 'string' },
        },
        required: ['name', 'passed'],
      },
    },
    tests_added: { type: 'array', items: { type: 'string' } },
    code_fixes_needed: { type: 'array', items: { type: 'string' } },
  },
  required: ['all_green', 'suites'],
}

const REVIEWERS = [
  { key: 'correctness', type: 'reviewer-correctness' },
  { key: 'security', type: 'reviewer-security' },
  { key: 'conventions', type: 'reviewer-conventions' },
]

const show = (obj) => JSON.stringify(obj, null, 2)

// Condensed role briefs, used only when a custom agent type from .claude/agents
// is not registered in this session (they load at session start). After a
// restart the full definitions in .claude/agents/*.md are used instead.
const ROLE_BRIEFS = {
  planner:
    'ROLE: read-only planner for Lectern. Do not edit files. Read the code the task touches and its ' +
    'neighbours (router, module, model, frontend api wrapper and page, tests, docs) and the test setup ' +
    '(which Playwright tier runs in CI, fixtures, backend/tests/conftest.py). Name real files and ' +
    'functions. Apply CLAUDE.md rules explicitly (migrations, manifest convergence, path safety, events, ' +
    'provider boundaries, API type mirroring, docs). Prefer the smallest design that fits existing ' +
    'patterns. If a real choice changes the work materially, put it under needs_decision with options ' +
    'and a recommendation; routine calls go under assumptions. If the task already is a detailed brief, ' +
    'validate and correct it rather than rewriting. Return summary, brief (Backend, Frontend, Tests, ' +
    'Docs, Acceptance; concrete), files, risks, assumptions, needs_decision.',
  implementer:
    'ROLE: implementer on the Lectern dev team. Build exactly what the task asks. Read the named files ' +
    'and their neighbours first and match existing patterns. Follow CLAUDE.md: add-column-only migrations ' +
    'with a test_db_migration.py case; convergent manifest ops; path confinement via the guards in ' +
    'servers/files.py and backups.py; record outcomes via events.py; mirror backend schemas in ' +
    'frontend/src/api; shared Modal/Toasts; phone-width layouts; update docs/functional.md and ' +
    'docs/technical.md when behaviour or API changes. Write tests with the change (mark process/network ' +
    'tests @pytest.mark.integration). Before reporting run from the repo root: ' +
    'backend/.venv/bin/ruff check backend; backend/.venv/bin/python -m pytest -q backend/tests; ' +
    'npm --prefix frontend run lint; npm --prefix frontend test. Never commit, never edit CLAUDE.md or ' +
    '.github/. Report summary, files_changed, tests_added, checks_passed, open_questions.',
  'reviewer-correctness':
    'ROLE: read-only correctness reviewer. Do not edit files. Use git diff/git status and read code. Hunt ' +
    'real bugs: wrong logic, missed edge cases, None handling, blocking calls in the event loop, ' +
    'un-awaited tasks, server lifecycle gaps, manifest non-convergence, missing migration tests, ' +
    'unrecorded events, stale frontend state, tests that would pass on a revert. Report only findings ' +
    'with a concrete failure scenario; severity high = data loss or broken running server, medium = ' +
    'wrong user-visible behaviour, low otherwise. An empty list is a valid result.',
  'reviewer-security':
    'ROLE: read-only security reviewer. Do not edit files. Lectern has no auth by design on a trusted LAN; ' +
    'that is not a finding. Look for: paths from requests, archives, manifests or provider data escaping ' +
    'the server directory (absolute, .., symlinks, joined-before-checked); zip-slip on extraction; ' +
    'injection into Java command lines, JVM flags, server.properties or console commands; unverified ' +
    'downloads or SSRF through provider URLs; writes that can clobber .lectern/, the DB or another ' +
    'server; dangerous backup/delete targets; secrets in logs or responses. Report only findings with a ' +
    'concrete exploit path. An empty list is a valid result.',
  'reviewer-conventions':
    'ROLE: read-only conventions and maintainability reviewer. Do not edit files. CLAUDE.md is the ' +
    'checklist; read the relevant parts of docs/functional.md and docs/technical.md. Check: docs and ' +
    'roadmap updated when behaviour or API changed; server types via servers/types.py and content ' +
    'sources via providers/base.py; async-first Python 3.11+, ruff clean, integration markers; frontend ' +
    'API types mirror backend schemas, shared Modal/Toasts, phone layout; events recorded; duplicated ' +
    'logic, dead code, naming that does not match neighbours; scope creep. Report concrete, actionable ' +
    'items only. An empty list is a valid result.',
  'docs-writer':
    'ROLE: docs writer. Edit only docs/*.md and README.md; never code, tests or CLAUDE.md. Read git diff ' +
    'and git status, then: docs/functional.md (an F-… requirement for new/changed behaviour, roadmap ' +
    'table in §3, F-APP-1 for new app settings); docs/technical.md (endpoints under §7 with method, ' +
    'path, payload, status codes; on-disk layout; tunables with env name and default; new modules; ' +
    'mechanics bullets); README.md (Features list, Configuration section). Match the terse, specific ' +
    'voice of the existing text; no marketing, no changelog, no version bumps. Report summary, ' +
    'files_changed, gaps.',
  qa:
    'ROLE: QA. Prove the change works and is covered; report honestly when it is not. From the repo root ' +
    'run in order and read every output: backend/.venv/bin/ruff check backend; ' +
    'backend/.venv/bin/python -m pytest -q backend/tests; npm --prefix frontend run lint; ' +
    'npm --prefix frontend test; npm --prefix frontend run build; npm --prefix frontend run test:e2e. ' +
    'Run docker build -t lectern-qa . only if the diff touches Dockerfile, docker-compose*.yml, ' +
    'backend/pyproject.toml or frontend/package*.json. Do not run test:e2e:full. Add missing tests ' +
    '(test files only); if a failure needs application code, describe it under code_fixes_needed. ' +
    'A failing test is a finding, not something to re-run until green. Report all_green, suites ' +
    '(name, passed, detail), tests_added, code_fixes_needed.',
}

async function role(type, prompt, opts) {
  try {
    return await agent(prompt, { ...opts, agentType: type })
  } catch (e) {
    if (!String(e && e.message).includes('not found')) throw e
    log(`agent type '${type}' is not registered in this session (restart Claude Code to load .claude/agents); using inline brief`)
    return agent(`${ROLE_BRIEFS[type]}\n\n---\n\n${prompt}`, opts)
  }
}

// `brief` is what the implementer receives; the planner may replace it.
let brief = task

const planPrompt = () =>
  `Plan this task for Lectern's implementer.\n\nTask:\n${task}\n\n` +
  (opt.decisions
    ? `The user answered the planner's earlier questions as follows; honour them and leave needs_decision empty unless something new comes up:\n${opt.decisions}\n\n`
    : '') +
  `Work on the current branch (git status / git diff show uncommitted work that is part of the context, not something to undo). Do not edit files.`

const reviewPrompt = (impl) =>
  `Task given to the implementer:\n${brief}\n\nImplementer report:\n${show(impl)}\n\n` +
  `Review the uncommitted working-tree changes: run \`git diff\` and \`git status\`, ` +
  `and read any untracked files the report lists.`

const refutePrompt = (f) =>
  `A reviewer (${f.lens} lens) reported this finding on the uncommitted working-tree changes:\n${show(f)}\n\n` +
  `Task context:\n${brief}\n\n` +
  `Try to refute it. Read the code at that location and the callers. The finding stands only if the ` +
  `failure scenario is reachable in practice. Set refuted=true if the scenario cannot happen, is already ` +
  `handled elsewhere, or is outside what the task changed. Default to refuted=true if you are unsure. ` +
  `Do not edit files.`

const fixPrompt = (impl, findings) =>
  `You previously implemented this task:\n${brief}\n\nYour report was:\n${show(impl)}\n\n` +
  `Independent review confirmed these findings. Fix each one, keep the fix minimal, re-run the fast checks, ` +
  `and report again in the same shape. If you disagree with a finding, say why under open_questions ` +
  `instead of silently skipping it.\n\nFindings:\n${show(findings)}`

const docsPrompt = (impl) =>
  `Bring the documentation in line with this change.\n\nTask:\n${brief}\n\nLatest implementer report:\n${show(impl)}`

const qaPrompt = (impl) =>
  `Verify this change end to end.\n\nTask:\n${brief}\n\nLatest implementer report:\n${show(impl)}`

const qaFixPrompt = (impl, qa) =>
  `You previously implemented this task:\n${brief}\n\nYour report was:\n${show(impl)}\n\n` +
  `QA ran the full suite and it is red. Fix the application code so the suite passes; do not weaken or ` +
  `delete tests. Report again in the same shape.\n\nQA report:\n${show(qa)}`

// ---- Plan ------------------------------------------------------------------
let plan = null
if (opt.plan !== false) {
  phase('Plan')
  plan = await role('planner', planPrompt(), {
    model: PLANNER_MODEL, effort: 'high', phase: 'Plan', schema: PLAN, label: 'plan',
  })
  if (!plan) throw new Error('Planner returned nothing')
  if (plan.needs_decision.length && !opt.proceed) {
    log(`planner has ${plan.needs_decision.length} open decision(s); stopping before implementation`)
    return { task, plan, stopped_for_decisions: true, ready_to_commit: false }
  }
  brief = plan.brief
} else {
  log('planning skipped (args.plan === false); the task is used as the brief')
}

// ---- Implement -------------------------------------------------------------
phase('Implement')
let impl = await role('implementer', brief, {
  model: MODEL, phase: 'Implement', schema: IMPL, label: 'implement',
})
if (!impl) throw new Error('Implementer returned nothing')
const history = [impl]

// ---- Review / Verify / Fix rounds -----------------------------------------
let reviewClean = false
let lastConfirmed = []
for (let round = 1; round <= maxRounds; round++) {
  // Barrier is deliberate: findings are deduplicated across reviewers before
  // paying for a refuter each, and an empty round ends the loop.
  const raw = (await parallel(REVIEWERS.map((r) => () =>
    role(r.type, reviewPrompt(impl), {
      model: MODEL, phase: 'Review', schema: FINDINGS, label: `review:${r.key} r${round}`,
    }).then((res) => (res ? res.findings.map((f) => ({ ...f, lens: r.key })) : [])),
  ))).filter(Boolean).flat()

  const seen = new Set()
  const fresh = raw.filter((f) => {
    const key = `${f.file}:${f.line || 0}`
    if (seen.has(key)) return false
    seen.add(key)
    return true
  })
  log(`review round ${round}: ${raw.length} findings, ${fresh.length} after dedup by file:line`)
  if (!fresh.length) { reviewClean = true; lastConfirmed = []; break }

  const judged = (await parallel(fresh.map((f) => () =>
    agent(refutePrompt(f), {
      model: MODEL, effort: 'high', phase: 'Verify', schema: VERDICT, label: `verify:${f.file}`,
    }).then((v) => ({ ...f, refuted: v ? v.refuted : true, verdict_reason: v ? v.reason : 'refuter failed' })),
  ))).filter(Boolean)
  lastConfirmed = judged.filter((f) => !f.refuted)
  log(`review round ${round}: ${lastConfirmed.length}/${fresh.length} findings survived refutation`)
  if (!lastConfirmed.length) { reviewClean = true; break }

  impl = await role('implementer', fixPrompt(impl, lastConfirmed), {
    model: MODEL, phase: 'Fix', schema: IMPL, label: `fix r${round}`,
  })
  if (!impl) throw new Error(`Implementer returned nothing in fix round ${round}`)
  history.push(impl)
  if (round === maxRounds) log(`review rounds exhausted after ${maxRounds}; last fixes were not re-reviewed`)
}

// ---- Docs ------------------------------------------------------------------
phase('Docs')
const docs = await role('docs-writer', docsPrompt(impl), {
  model: MODEL, phase: 'Docs', schema: DOCS, label: 'docs',
})

// ---- QA --------------------------------------------------------------------
phase('QA')
let qa = await role('qa', qaPrompt(impl), {
  model: MODEL, phase: 'QA', schema: QA, label: 'qa',
})
if (qa && !qa.all_green) {
  log(`QA red: ${qa.suites.filter((s) => !s.passed).map((s) => s.name).join(', ')}; one repair round`)
  const repaired = await role('implementer', qaFixPrompt(impl, qa), {
    model: MODEL, phase: 'QA', schema: IMPL, label: 'qa-fix',
  })
  if (repaired) { impl = repaired; history.push(repaired) }
  qa = await role('qa', qaPrompt(impl), {
    model: MODEL, phase: 'QA', schema: QA, label: 'qa rerun',
  })
}

return {
  task,
  plan,
  review_clean: reviewClean,
  findings_fixed_but_not_re_reviewed: reviewClean ? [] : lastConfirmed,
  implementation_rounds: history,
  docs,
  qa,
  ready_to_commit: Boolean(reviewClean && qa && qa.all_green),
}
