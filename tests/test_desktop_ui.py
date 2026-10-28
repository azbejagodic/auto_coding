"""Headless dashboard contract tests; no native window or real runner."""
import shutil
import subprocess
import tempfile
import unittest
import sys
from html.parser import HTMLParser
from pathlib import Path

# Resolve imports from this checkout even when an older desktop build is installed.
REPOSITORY = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPOSITORY / "src"))
from auto_coding import desktop_app
from auto_coding.desktop_app import HTML


class DesktopUITests(unittest.TestCase):
    def test_import_is_from_this_checkout(self):
        self.assertEqual(Path(desktop_app.__file__).resolve(),
                         REPOSITORY / "src" / "auto_coding" / "desktop_app.py")

    def test_controls_are_visible_and_scoped_to_bridge(self):
        class Controls(HTMLParser):
            def __init__(self):
                super().__init__()
                self.ids = {}

            def handle_starttag(self, tag, attrs):
                attrs = dict(attrs)
                if "id" in attrs:
                    self.ids[attrs["id"]] = tag

        parser = Controls()
        parser.feed(HTML)
        for name in ("browse-project", "browse-plan", "open-run", "start", "resume",
                     "connect-github", "list-github", "select-branch", "new-branch",
                     "create-branch", "cancel-branch", "fetch-github", "push-approved",
                     "nav-overview", "nav-tasks", "nav-logs"):
            self.assertEqual(parser.ids[name], "button")
        self.assertEqual(parser.ids["github-repo"], "input")
        self.assertEqual(parser.ids["auto-push"], "input")
        self.assertIn('id="auto-push" type="checkbox" checked> Automatically push approved tasks', HTML)
        self.assertEqual(parser.ids["github-branch"], "select")
        self.assertEqual(parser.ids["branch-base"], "select")
        self.assertEqual(parser.ids["branch-base-confirm"], "input")
        self.assertIn('Local folder fallback', HTML)
        self.assertIn('Push Approved Commits', HTML)
        self.assertNotIn('Merge to main</button>', HTML)
        self.assertEqual(parser.ids["attempts"], "input")
        for method in ("select_repo", "select_plan", "open_run", "start", "resume"):
            self.assertIn(method, HTML)
        self.assertIn('min="1" max="10" step="1" value="3" required', HTML)
        self.assertIn("Local Codex CLI authentication is required", HTML)
        self.assertIn("Closing this window leaves the runner alive", HTML)
        self.assertNotIn("innerHTML", HTML)
        self.assertNotIn("Read-only viewer", HTML)

    def test_layout_accessibility_and_text_safety_contract(self):
        for token in ('width:210px', 'margin-left:210px', 'font:16px/1.5',
                      'overflow-wrap:anywhere', ':focus-visible',
                      'prefers-reduced-motion:reduce', 'grid-template-columns:minmax(0,1fr)',
                      'aria-label="Main navigation"', 'aria-labelledby="project-label"',
                      'aria-labelledby="plan-label"', 'aria-describedby="preflight start-reason attempts-error"',
                      'id="project-path"', 'id="plan-path"', 'id="run-setup"',
                      'Project folder', 'Task plan', 'Repository', 'Open Run',
                      "'Awaiting recovery'", "'Completed', color:'green'",
                      "'Idle', color:'muted'", "'Unknown run state'",
                      'Run downloads', 'Download ZIP',
                      "content.setAttribute('tabindex', '0')"):
            self.assertIn(token, HTML)
        for unsafe in ('innerHTML', 'outerHTML', 'insertAdjacentHTML', '<script src=',
                       'https://', 'Unknown / Unknown'):
            self.assertNotIn(unsafe, HTML)

    def test_numeric_keyboard_and_compact_overview_contract(self):
        class Markup(HTMLParser):
            def __init__(self):
                super().__init__()
                self.nodes = {}
                self.labels = []

            def handle_starttag(self, tag, attrs):
                attrs = dict(attrs)
                if "id" in attrs:
                    self.nodes[attrs["id"]] = (tag, attrs)
                if tag == "label":
                    self.labels.append(attrs)

        markup = Markup()
        markup.feed(HTML)
        number = markup.nodes["attempts"][1]
        self.assertEqual(number["type"], "number")
        self.assertTrue(any(label.get("for") == "attempts" for label in markup.labels))
        self.assertEqual(number["aria-describedby"], "attempts-hint attempts-error")
        for name in ("attempts", "start", "resume", "nav-overview", "nav-tasks", "nav-logs"):
            tag, attrs = markup.nodes[name]
            self.assertIn(tag, ("input", "button"))  # Native Tab/Enter/Space semantics.
            self.assertNotIn("tabindex", attrs)  # No positive or negative tab-order overrides.
        for name in ("header-state", "attempts-error", "preflight"):
            self.assertEqual(markup.nodes[name][1]["role"], "status")
            self.assertEqual(markup.nodes[name][1]["aria-live"], "polite")
        self.assertNotIn('open', markup.nodes["launch-details"][1])
        for name in ("secondary-controls", "local-project", "launch-details"):
            self.assertEqual(markup.nodes[name][0], "details")
            self.assertNotIn("open", markup.nodes[name][1])
        self.assertIn("checked", markup.nodes["auto-push"][1])
        # Primary form order remains native.
        order = list(markup.nodes)
        for first, second in zip(("github-repo", "github-branch", "plan-path", "attempts", "start"),
                                 ("github-branch", "plan-path", "attempts", "start", "secondary-controls")):
            self.assertLess(order.index(first), order.index(second))
        for text in ("Repository and branch", "1. Repository", "2. Task plan", "3. Attempts", "4. Start",
                     "Git actions and saved runs", "Launch safeguards and diagnostics", "Run overview"):
            self.assertNotIn(text, HTML)
        self.assertNotIn("Ready for your first run", HTML)
        self.assertNotIn("const steps", HTML)
        self.assertEqual(HTML.count('require native confirmation.'), 1)

    def test_overview_ends_with_checkbox_and_only_status_cards(self):
        setup = HTML.split('<section class="panel" id="run-setup"', 1)[1].split('</section>', 1)[0]
        tail = setup.split('<label class="push-policy">', 1)[1]
        self.assertEqual(tail.strip(), '<input id="auto-push" type="checkbox" checked> Automatically push approved tasks</label>')
        for name in ('secondary-controls', 'local-project', 'launch-details', 'list-github'):
            self.assertNotIn('id="' + name + '"', setup)
            logs = HTML.split('<section id="logs-tools"', 1)[1].split('</section>', 1)[0]
            self.assertIn('id="' + name + '"', logs)
        selection = HTML.split('<section id="run-selection"', 1)[1].split('</section>', 1)[0]
        self.assertIn('<button id="open-run">Open Run</button>', selection)
        self.assertEqual(HTML.count('id="open-run"'), 1)
        self.assertNotIn('id="open-run"', setup)
        self.assertNotIn('id="open-run"', logs)
        for removed in ('auto-push-note', 'run-history', 'More controls and diagnostics', 'New run: automatic push'):
            self.assertNotIn(removed, HTML)
        self.assertIn("[['RUN STATUS', status], ['COMPLETED TASKS', count], ['CHILD PROCESS', child]]", HTML)

    def test_rendered_form_keyboard_secondary_actions_and_small_viewports(self):
        try:
            from playwright.sync_api import sync_playwright
        except ImportError:
            self.skipTest("Playwright absent; rendered viewport checks unavailable")
        with sync_playwright() as playwright:
            try:
                browser = playwright.chromium.launch(headless=True)
            except Exception as exc:
                self.skipTest(f"Chromium unavailable: {exc}")
            try:
                page = browser.new_page()
                failures = []
                page.on("pageerror", lambda error: failures.append(str(error)))
                # All bridge data is local and synthetic. No GitHub or Codex calls.
                page.route("**/*", lambda route: route.abort())
                page.set_content(HTML)
                page.evaluate("""() => {
                  window.calls = [];
                  window.fixture = {root:'', events:[], state_error:null, controller:{active:false, uncertain:false,
                    repo:null, plan:null, start_ready:false, resume_ready:false,
                    push:{eligible:false, reason:'Connect a managed GitHub repository with approved local checkpoints to publish.'},
                    preflight:{branch:'feature/ui', clean:true, errors:['Select a Git repository root.']}}};
                  window.pywebview = {api:{
                    snapshot:async () => structuredClone(window.fixture),
                    connect_github:async repo => {
                      calls.push(['connect_github', repo]);
                      Object.assign(fixture.controller, {repo:'C:/workspace', repository:repo,
                        push:{eligible:false, reason:'No unpublished approved checkpoints on this branch.'},
                        github:{local:['main','feature/ui'], remote:['origin/main','origin/feature/ui']}});
                      fixture.controller.preflight.errors = [];
                      return {ok:true};
                    },
                    checkout_github_branch:async (...args) => {calls.push(['branch', ...args]); return {ok:true};},
                    create_github_branch:async (...args) => {calls.push(['create', ...args]); return {ok:true};},
                    select_plan:async () => {
                      calls.push(['select_plan']); fixture.controller.plan = 'C:/' + 'long path/'.repeat(30) + '<script>bad()</script>.md';
                      fixture.controller.start_ready = true; return {ok:true};
                    },
                    start:async (...args) => {calls.push(['start', ...args]); return {ok:false, cancelled:true};},
                    resume:async (...args) => {calls.push(['resume', ...args]); return {ok:false, cancelled:true};},
                    push_approved_commits:async () => {calls.push(['push']); return {ok:true};},
                    select_repo:async () => {calls.push(['select_repo']); return {ok:false, cancelled:true};},
                    fetch_github:async (...args) => {calls.push(['fetch', ...args]); return {ok:true};},
                    github_repositories:async () => ({ok:true, available:true, repositories:['owner/other', '<img src=x onerror=bad()>']}),
                    open_run:async () => ({ok:false, cancelled:true})
                  }};
                  window.dispatchEvent(new Event('pywebviewready'));
                }""")
                page.wait_for_function("document.getElementById('connect-github').disabled === false")
                for width, height in ((960, 600), (1000, 620), (1366, 680), (1280, 640), (640, 480), (390, 720)):
                    page.set_viewport_size({'width': width, 'height': height})
                    self.assertTrue(page.evaluate('document.documentElement.scrollWidth <= innerWidth'))
                    self.assertEqual(page.locator('#app .card').count(), 3)
                    if width >= 960:
                        self.assertTrue(page.evaluate('document.documentElement.scrollHeight <= innerHeight'), f'{width}x{height}')
                        for box in page.locator('#app .card').all():
                            bounds = box.bounding_box()
                            self.assertLessEqual(bounds['y'] + bounds['height'], height)
                for name in ('github-state', 'start-reason', 'resume-reason', 'push-reason', 'preflight'):
                    self.assertEqual(page.locator('#' + name).inner_text(), '')
                for text in ('Connect a repository to choose a feature branch.', 'Connect a repository first.',
                             'Select a matching stopped run to resume.',
                             'Connect a managed GitHub repository with approved local checkpoints to publish.',
                             'Select a Git repository root.',
                             'Ready. Select a project and task plan, or open an existing run for inspection.'):
                    self.assertNotIn(text, page.locator('body').inner_text())
                page.set_viewport_size({'width': 1366, 'height': 680})
                for name in ('github-repo', 'github-branch', 'plan-path', 'browse-plan', 'attempts',
                             'fetch-github', 'select-branch', 'new-branch', 'start', 'resume',
                             'push-approved', 'auto-push'):
                    self.assertTrue(page.locator('#' + name).is_visible(), name)
                self.assertFalse(page.locator('#header-context').is_visible())
                for removed in ('Repository and branch', '1. Repository', '2. Task plan', '3. Attempts',
                                '4. Start', 'Run overview', 'Local developer workspace',
                                'Current task'):
                    self.assertNotIn(removed, page.locator('main').inner_text())
                # Native DOM order follows the primary form; collapsed actions are skipped.
                page.locator('#github-repo').focus()
                page.keyboard.press('Tab')
                self.assertEqual(page.evaluate('document.activeElement.id'), 'connect-github')
                page.locator('#github-repo').fill('owner/repo')
                page.locator('#connect-github').click()
                page.wait_for_function("fixture.controller.repository === 'owner/repo' && !document.getElementById('select-branch').disabled")
                self.assertEqual(page.evaluate('document.activeElement.id'), 'connect-github')
                page.locator('#github-branch').select_option('feature/ui')
                self.assertEqual(page.evaluate('calls.length'), 1)
                page.locator('#github-branch').focus()
                page.evaluate("""async () => {
                  fixture.events.push({actor:'controller', event:'status', outcome:'idle'});
                  fixture.controller.github.remote.push('origin/feature/newly-fetched');
                  await refresh(false);
                }""")
                self.assertEqual(page.locator('#github-branch').input_value(), 'feature/ui')
                self.assertEqual(page.evaluate('document.activeElement.id'), 'github-branch')
                self.assertEqual(page.evaluate('calls.length'), 1, 'Polling selections never launches work')
                page.locator('#select-branch').click()
                page.locator('#browse-plan').click()
                page.wait_for_function("!document.getElementById('start').disabled")
                self.assertEqual(page.evaluate('document.activeElement.id'), 'browse-plan')
                self.assertIn('<script>bad()</script>', page.locator('#plan-path').inner_text())
                self.assertEqual(page.locator('#plan-path script').count(), 0)
                self.assertTrue(page.locator('#auto-push').is_checked())
                page.locator('#auto-push').check()
                page.evaluate('refresh(false)')
                self.assertTrue(page.locator('#auto-push').is_checked())
                page.evaluate("""() => {
                  fixture.controller.plan += '.next.md';
                  return refresh(false);
                }""")
                self.assertTrue(page.locator('#auto-push').is_checked())
                page.locator('#github-repo').focus()
                for name in ('connect-github', 'github-branch', 'fetch-github', 'select-branch', 'new-branch',
                             'plan-path', 'browse-plan', 'attempts', 'start'):
                    page.keyboard.press('Tab')
                    self.assertEqual(page.evaluate('document.activeElement.id'), name)
                # Resume and Push remain visible even while disabled; neither is in a disclosure.
                for name in ('resume', 'push-approved', 'fetch-github', 'select-branch', 'new-branch'):
                    self.assertTrue(page.locator('#' + name).is_visible())
                    self.assertEqual(page.locator('details #' + name).count(), 0)
                page.locator('#nav-logs').click()
                self.assertFalse(page.locator('#logs-tools').is_visible())
                self.assertIn('No saved runs found.', page.locator('#app').inner_text())
                page.locator('#nav-overview').click()
                self.assertFalse(page.locator('#secondary-controls').is_visible())
                # Verify native disclosures really expose local selection and
                # recovery on Tasks, rather than merely retaining bridge APIs.
                page.locator('#nav-tasks').click()
                page.locator('#secondary-controls > summary').click()
                page.locator('#local-project > summary').click()
                self.assertTrue(page.locator('#browse-project').is_visible())
                page.locator('#browse-project').click()
                page.wait_for_function("calls.at(-1)[0] === 'select_repo'")
                page.evaluate("""async () => {
                  fixture.controller.preflight.locks = [{path:'managed/project.job.lock', recoverable:true}];
                  pywebview.api.recover_workspace_lock = async suffix => {
                    calls.push(['recover_workspace_lock', suffix]);
                    fixture.controller.preflight.locks = [];
                    return {ok:true, archive:'managed/archived.lock'};
                  };
                  await refresh(false);
                }""")
                page.locator('#launch-details > summary').click()
                page.locator('#lock-recovery button').click()
                page.wait_for_function("calls.at(-1)[0] === 'recover_workspace_lock'")
                self.assertEqual(page.evaluate('calls.at(-1)[1]'), '.job.lock')
                page.wait_for_function("document.getElementById('lock-recovery').children.length === 0")
                page.evaluate("document.getElementById('secondary-controls').open = false")
                page.locator('#nav-overview').click()
                # Measure the steady Overview after transient selection feedback.
                page.evaluate("message(''); window.scrollTo(0, 0)")
                for width, height in ((960, 620), (1000, 620), (1366, 680), (1280, 720), (1600, 900), (640, 480), (390, 720)):
                    page.set_viewport_size({'width': width, 'height': height})
                    self.assertTrue(page.evaluate('document.documentElement.scrollWidth <= innerWidth'), str(width))
                    boxes = []
                    for name in ('github-repo', 'github-branch', 'fetch-github', 'select-branch', 'new-branch',
                                 'plan-path', 'browse-plan', 'attempts', 'start', 'resume', 'push-approved'):
                        box = page.locator('#' + name).bounding_box()
                        self.assertGreaterEqual(box['x'], 0)
                        self.assertLessEqual(box['x'] + box['width'], width + 1)
                        self.assertGreaterEqual(box['height'], 40)
                        for other in boxes:
                            overlap_x = min(box['x'] + box['width'], other['x'] + other['width']) - max(box['x'], other['x'])
                            overlap_y = min(box['y'] + box['height'], other['y'] + other['height']) - max(box['y'], other['y'])
                            self.assertFalse(overlap_x > 0 and overlap_y > 0, 'Primary controls overlap')
                        boxes.append(box)
                    if width >= 960:
                        start_size = page.locator('#start').bounding_box()
                        resume_size = page.locator('#resume').bounding_box()
                        push_size = page.locator('#push-approved').bounding_box()
                        self.assertEqual(start_size['width'], resume_size['width'])
                        self.assertEqual(start_size['height'], resume_size['height'])
                        self.assertEqual(start_size['height'], push_size['height'])
                        start = page.locator('#start').bounding_box()
                        self.assertLessEqual(start['y'] + start['height'], height, 'Primary launch visible without scrolling')
                        self.assertLess(page.locator('#github-branch').bounding_box()['y'], 400)
                    self.assertLessEqual(page.locator('#plan-path').bounding_box()['height'], 96)
                    cards = page.locator('#app .card')
                    self.assertEqual(cards.count(), 3)
                    self.assertEqual(page.locator('#app .card .label').all_text_contents(),
                                     ['RUN STATUS', 'COMPLETED TASKS', 'CHILD PROCESS'])
                    positions = [cards.nth(i).bounding_box() for i in range(3)]
                    if width >= 960:
                        self.assertEqual(len({box['y'] for box in positions}), 1)
                        self.assertTrue(all(box['y'] + box['height'] <= height for box in positions),
                                        f'All three status cards fit without scrolling at {width}x{height}: {positions}')
                    else:
                        self.assertEqual(len({box['y'] for box in positions}), 3)
                    self.assertEqual(page.locator('#run-setup > :last-child').get_attribute('class'), 'push-policy')
                    self.assertFalse(page.locator('#logs-tools').is_visible())
                # Native keyboard activation reaches the same confirmation boundary.
                page.locator('#start').focus()
                page.keyboard.press('Enter')
                page.wait_for_function("document.getElementById('message').textContent.includes('Cancelled')")
                self.assertFalse(page.evaluate('fixture.controller.active'))
                self.assertEqual(page.evaluate("calls.filter(c => c[0] === 'start')"), [['start', 3, True]])
                self.assertEqual(page.evaluate('document.activeElement.id'), 'start')
                page.keyboard.press('Space')
                page.wait_for_function("calls.filter(c => c[0] === 'start').length === 2 && !document.getElementById('start').disabled")
                self.assertFalse(page.evaluate('fixture.controller.active'))
                self.assertTrue(page.locator('#fetch-github').is_visible())
                page.locator('#fetch-github').click()
                page.locator('#nav-logs').click()
                page.evaluate("document.getElementById('list-github').click()")
                page.evaluate("() => { const select = document.getElementById('github-repositories'); select.value = 'owner/other'; select.dispatchEvent(new Event('change')); }")
                page.wait_for_function("fixture.controller.repository === 'owner/other'")
                self.assertEqual(page.locator('#github-repositories img').count(), 0)
                page.locator('#nav-overview').click()
                page.locator('#new-branch').click()
                self.assertEqual(page.evaluate('document.activeElement.id'), 'branch-name')
                page.locator('#cancel-branch').click()
                self.assertEqual(page.evaluate('document.activeElement.id'), 'new-branch')
                page.locator('#new-branch').click()
                page.locator('#branch-name').fill('feature/new')
                page.locator('#branch-base').focus()
                page.evaluate("""() => {
                  fixture.controller.github.remote.push('origin/feature/another');
                  return refresh(false);
                }""")
                self.assertTrue(page.locator('#branch-name').is_visible())
                self.assertEqual(page.locator('#branch-name').input_value(), 'feature/new')
                self.assertEqual(page.locator('#branch-base').input_value(), 'main')
                self.assertEqual(page.evaluate('document.activeElement.id'), 'branch-base')
                self.assertTrue(page.locator('#create-branch').is_disabled())
                page.locator('#branch-base-confirm').check()
                page.locator('#create-branch').click()
                page.wait_for_function("calls.some(c => c[0] === 'create') && document.getElementById('branch-form').hidden")
                self.assertEqual(page.evaluate("calls.find(c => c[0] === 'create')"),
                                 ['create', 'owner/other', 'feature/new', 'main', True])
                page.evaluate("""() => {
                  fixture.controller.resume_ready = true;
                  fixture.controller.push = {eligible:true};
                  return refresh(false);
                }""")
                page.locator('#resume').click()
                page.wait_for_function("document.getElementById('message').textContent.includes('Cancelled')")
                self.assertEqual(page.evaluate("calls.find(c => c[0] === 'resume')"), ['resume', 3])
                self.assertFalse(page.evaluate('fixture.controller.active'))
                page.locator('#start').focus()
                page.keyboard.press('Tab')
                self.assertEqual(page.evaluate('document.activeElement.id'), 'resume')
                page.keyboard.press('Tab')
                self.assertEqual(page.evaluate('document.activeElement.id'), 'push-approved')
                page.locator('#push-approved').click()
                page.wait_for_function("calls.some(c => c[0] === 'push') && !document.getElementById('browse-project').disabled")
                page.locator('#nav-logs').click()
                # Legacy selection handlers remain available to bridge integrations.
                page.evaluate("document.getElementById('browse-project').click()")
                page.wait_for_function("document.getElementById('logs-message').textContent.includes('Cancelled')")
                self.assertTrue(page.locator('#logs-message').is_visible())
                self.assertEqual(page.evaluate('fixture.controller.repo'), 'C:/workspace')
                self.assertEqual(page.locator('#project-path').inner_text(), 'C:/workspace')
                page.locator('#nav-overview').click()
                # Diagnostics and secondary actions must wrap arbitrary repository data too.
                page.evaluate("""() => {
                  fixture.controller.preflight.errors = ['blocked-' + 'x'.repeat(600) + '<img src=x onerror=bad()>'];
                  fixture.controller.start_ready = false;
                  fixture.controller.push = {eligible:false, reason:'checkpoint-' + 'y'.repeat(600)};
                  return refresh(false);
                }""")
                for width, height in ((1280, 720), (1600, 900), (390, 720)):
                    page.set_viewport_size({'width': width, 'height': height})
                    self.assertTrue(page.evaluate('document.documentElement.scrollWidth <= innerWidth'), str(width))
                    self.assertTrue(page.locator('#start').is_disabled())
                    self.assertIn('<img src=x onerror=bad()>', page.locator('#preflight').inner_text())
                    self.assertEqual(page.locator('#preflight img, #start-reason img').count(), 0)
                for view in ('tasks', 'logs', 'overview'):
                    page.locator('#nav-' + view).click()
                    self.assertEqual(page.locator('#nav-' + view).get_attribute('aria-current'), 'page')
                    self.assertEqual(page.locator('#run-setup').is_visible(), view == 'overview')
                self.assertEqual(failures, [])
            finally:
                browser.close()

    def test_tasks_compact_rendering_keyboard_and_evidence(self):
        try:
            from playwright.sync_api import sync_playwright
        except ImportError:
            self.skipTest("Playwright absent; rendered Tasks checks unavailable")
        with sync_playwright() as playwright:
            try:
                browser = playwright.chromium.launch(headless=True)
            except Exception as exc:
                self.skipTest(f"Chromium unavailable: {exc}")
            try:
                page = browser.new_page(viewport={"width": 960, "height": 600})
                page.route("**/*", lambda route: route.abort())
                page.set_content(HTML)
                page.evaluate("""() => {
                  window.fixture = {root:'saved/run', exists:true, task_count:4,
                    task_ids:['Layout','Progress','Evidence','Keyboard'], events:[],
                    controller:{active:false, uncertain:false, logs:'saved/run', preflight:{errors:[]}},
                    state:{status:'stopped', current_task:'Progress', completed:['Layout'], history:[
                      {task:'Layout', attempt:1, review:{decision:'APPROVED', summary:'Full approval'}, commit:'abcdef0123456789'},
                      {task:'Progress', attempt:1, tests:[{index:1,exit_code:1,timed_out:true}],
                       review:null, feedback:'Original feedback <script>unsafe()</script>', extra:{preserve:'every byte'}}]}};
                  window.pywebview={api:{snapshot:async()=>structuredClone(fixture),
                    evidence:async path=>({ok:true,text:'Exact evidence '+path})}};
                  window.dispatchEvent(new Event('pywebviewready'));
                }""")
                page.locator('#nav-tasks').click()
                rows = page.locator('.task-row')
                self.assertEqual(rows.count(), 4)
                self.assertEqual(page.locator('.task-progress').first.inner_text(), '1 / 4 completed')
                self.assertLess(rows.last.bounding_box()['y'] + rows.last.bounding_box()['height'], 600)
                self.assertEqual(page.locator('.task-row[open]').count(), 0)
                self.assertNotIn('exit 1', page.locator('#app').inner_text())
                self.assertNotIn('timed out: true', page.locator('#app').inner_text())
                self.assertNotIn('Original feedback', page.locator('#app').inner_text())
                self.assertEqual(page.locator('.task-stop').count(), 1)
                summary = rows.nth(1).locator(':scope > summary')
                summary.focus()
                page.keyboard.press('Enter')
                self.assertTrue(rows.nth(1).evaluate('(row)=>row.open'))
                page.keyboard.press('Tab')
                self.assertEqual(page.evaluate('document.activeElement.textContent'), 'Feedback (original)')
                page.keyboard.press('Enter')
                self.assertIn('Original feedback', rows.nth(1).inner_text())
                self.assertEqual(page.locator('#app script').count(), 0)
                # Polling keeps native expander state and keyboard focus.
                page.evaluate("fixture.events.push({event:'stopped',task_id:'Progress',attempt:1,metadata:{reason:'test timeout'}}); refresh(false)")
                self.assertEqual(page.evaluate('document.activeElement.textContent'), 'Feedback (original)')
                self.assertTrue(page.locator('.task-row details').filter(has=page.locator('summary', has_text='Feedback (original)')).first.evaluate('(node)=>node.open'))
                page.locator('summary', has_text='Full attempt record').nth(1).click()
                self.assertIn('every byte', rows.nth(1).inner_text())
                for width, height in ((960,600), (640,480), (390,720)):
                    page.set_viewport_size({'width':width, 'height':height})
                    self.assertTrue(page.evaluate('document.documentElement.scrollWidth <= innerWidth'))
            finally:
                browser.close()

    def test_logs_compact_downloads_and_small_viewports(self):
        try:
            from playwright.sync_api import sync_playwright
        except ImportError:
            self.skipTest('Playwright absent')
        with sync_playwright() as playwright:
            try:
                browser = playwright.chromium.launch(headless=True)
            except Exception as exc:
                self.skipTest(f'Chromium unavailable: {exc}')
            try:
                page = browser.new_page(viewport={'width':960, 'height':600})
                failures = []
                page.on('pageerror', lambda error: failures.append(str(error)))
                page.route('**/*', lambda route: route.abort())
                page.set_content(HTML)
                page.evaluate("""() => {
                  window.calls = [];
                  window.runs = [];
                  window.fixture = {root:'', exists:false, events:[], controller:{active:false,
                    uncertain:false, preflight:{errors:[]}}};
                  window.pywebview = {api:{snapshot:async()=>structuredClone(fixture),
                    recent_runs:async()=>({runs:structuredClone(runs)}),
                    download_run:async path=>{calls.push(['download',path]); return {ok:true,message:'ZIP saved.'};}}};
                  window.dispatchEvent(new Event('pywebviewready'));
                }""")
                page.locator('#nav-logs').click()
                self.assertEqual(page.locator('#app').inner_text(), 'No saved runs found.')
                page.evaluate("""async () => {
                  runs = [{path:'C:/runs/old',run_id:'old-private-uuid',plan_filename:'older.json',status:'completed',time:'2026-10-24T12:00:00Z'},
                    {path:'C:/runs/new',run_id:'new-private-uuid',plan_filename:'current.json',status:'stopped',time:'2026-10-25T12:00:00Z'},
                    {path:'C:/runs/active',plan_filename:'active.json',status:'running',time:'2026-10-26T12:00:00Z'}];
                  fixture.events=[{event:'review_finished',reason:'Private feedback'}];
                  await refresh(false);
                }""")
                rows = page.locator('.run-row')
                self.assertEqual(rows.count(), 3)
                self.assertIn('active.json', rows.first.inner_text())
                self.assertTrue(rows.first.get_by_role('button', name='Download ZIP').is_disabled())
                self.assertIn('Available when run stops', rows.first.inner_text())
                self.assertEqual(page.locator('#app button').count(), 3)
                self.assertEqual(page.locator('#app details, #app pre, #app input').count(), 0)
                for text in ('Private feedback', 'private-uuid', 'Selected run', 'Recent activity', 'C:/runs'):
                    self.assertNotIn(text, page.locator('#app').inner_text())
                button = rows.nth(1).get_by_role('button', name='Download ZIP')
                button.focus()
                page.keyboard.press('Enter')
                page.wait_for_function("calls.some(c=>c[1]==='C:/runs/new')")
                self.assertIn('ZIP saved.', rows.nth(1).inner_text())
                self.assertEqual(page.locator('#page-title').inner_text(), 'Logs')
                self.assertTrue(page.locator('#page-title').is_visible())
                for width, height in ((960,600), (640,480), (390,720)):
                    page.set_viewport_size({'width':width,'height':height})
                    self.assertTrue(page.evaluate('document.documentElement.scrollWidth <= innerWidth'))
                    sizes = page.locator('.run-download').evaluate_all('(nodes)=>nodes.map(n=>n.getBoundingClientRect().width)')
                    self.assertEqual(len(set(sizes)), 1)
                self.assertFalse(page.locator('#logs-tools').is_visible())
                self.assertFalse(page.locator('#evidence-viewer').is_visible())
                self.assertFalse(page.locator('#open-run').is_visible())
                page.evaluate("""() => {
                  pywebview.api.open_run = async () => {
                    calls.push(['open_run']);
                    fixture.root = fixture.controller.logs = 'C:/runs/reopened';
                    fixture.exists = true;
                    fixture.task_ids = ['restored'];
                    fixture.state = {status:'stopped', completed:[], history:[]};
                    return {ok:true};
                  };
                }""")
                page.locator('#nav-tasks').click()
                self.assertTrue(page.locator('#open-run').is_visible())
                page.locator('#open-run').click()
                page.wait_for_function("fixture.root === 'C:/runs/reopened' && document.querySelector('.task-row')")
                self.assertIn('restored', page.locator('#app').inner_text())
                page.locator('#nav-overview').click()
                self.assertFalse(page.locator('#open-run').is_visible())
                self.assertEqual(failures, [])
            finally:
                browser.close()

    def test_actual_ui_buttons_navigation_polling_and_state_transitions(self):
        node = shutil.which("node")
        if not node:
            try:
                import playwright
                bundled = Path(playwright.__file__).parent / "driver" / "node.exe"
                if bundled.is_file():
                    node = str(bundled)
            except ImportError:
                pass
        if not node:
            self.skipTest("Node.js absent; headless UI interaction checks unavailable")
        script = HTML.split("<script>", 1)[1].split("</script>", 1)[0]
        fixture = Path(__file__).with_name("desktop_ui_harness.cjs")
        with tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parents[1]) as td:
            js = Path(td) / "dashboard.js"
            js.write_text(script, encoding="utf-8")
            syntax = subprocess.run([node, "--check", str(js)], capture_output=True,
                                    text=True, timeout=20)
            self.assertEqual(syntax.returncode, 0, syntax.stderr)
            for mode in ('normal', 'saved', 'stale', 'tasks', 'logs', 'reopen'):
                result = subprocess.run([node, str(fixture), str(js), mode], capture_output=True,
                                        text=True, encoding="utf-8", errors="replace", timeout=20)
                self.assertEqual(result.returncode, 0, mode + result.stdout + result.stderr)


if __name__ == "__main__":
    unittest.main()
