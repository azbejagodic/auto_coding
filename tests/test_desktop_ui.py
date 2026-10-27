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
        self.assertIn('id="auto-push" type="checkbox"> Automatically push approved tasks', HTML)
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
                      'aria-labelledby="plan-label"', 'aria-describedby="start-reason attempts-error"',
                      'id="project-path"', 'id="plan-path"', 'id="run-setup"',
                      'Project folder', 'Task plan', 'Repository', 'Open Run',
                      "'Awaiting recovery'", "'Completed', color:'green'",
                      "'Idle', color:'muted'", "'Unknown run state'",
                      'Search actor, outcome or event', '1 MB per file',
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
        for name in ("header-state", "attempts-error"):
            self.assertEqual(markup.nodes[name][1]["role"], "status")
            self.assertEqual(markup.nodes[name][1]["aria-live"], "polite")
        self.assertNotIn('open', markup.nodes["launch-details"][1])
        for name in ("secondary-controls", "local-project", "launch-details"):
            self.assertEqual(markup.nodes[name][0], "details")
            self.assertNotIn("open", markup.nodes[name][1])
        self.assertNotIn("checked", markup.nodes["auto-push"][1])
        # Primary form order remains native; secondary tools live in Logs.
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
        self.assertEqual(tail.strip(), '<input id="auto-push" type="checkbox"> Automatically push approved tasks</label>')
        for name in ('open-run', 'secondary-controls', 'local-project', 'launch-details', 'list-github'):
            self.assertNotIn('id="' + name + '"', setup)
            logs = HTML.split('<section id="logs-tools"', 1)[1].split('</section>', 1)[0]
            self.assertIn('id="' + name + '"', logs)
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
                  window.fixture = {root:'', events:[], controller:{active:false, uncertain:false,
                    repo:null, plan:null, start_ready:false, resume_ready:false,
                    preflight:{branch:'feature/ui', clean:true, errors:[]}}};
                  window.pywebview = {api:{
                    snapshot:async () => structuredClone(window.fixture),
                    connect_github:async repo => {
                      calls.push(['connect_github', repo]);
                      Object.assign(fixture.controller, {repo:'C:/workspace', repository:repo,
                        github:{local:['main','feature/ui'], remote:['origin/main','origin/feature/ui']}});
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
                self.assertFalse(page.locator('#auto-push').is_checked())
                page.locator('#auto-push').check()
                page.evaluate('refresh(false)')
                self.assertTrue(page.locator('#auto-push').is_checked())
                page.evaluate("""() => {
                  fixture.controller.plan += '.next.md';
                  return refresh(false);
                }""")
                self.assertFalse(page.locator('#auto-push').is_checked())
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
                page.locator('#secondary-controls > summary').click()
                self.assertTrue(page.locator('#list-github').is_visible())
                self.assertTrue(page.locator('#open-run').is_visible())
                page.locator('#launch-details > summary').click()
                self.assertTrue(page.locator('#preflight-details').is_visible())
                self.assertIn('feature/ui', page.locator('#preflight-details').inner_text())
                self.assertIn('Recent runs', page.locator('#app').inner_text())
                self.assertIn('Export DEBUG REPORT', page.locator('#app').inner_text())
                page.locator('#nav-overview').click()
                self.assertFalse(page.locator('#secondary-controls').is_visible())
                page.evaluate('window.scrollTo(0, 0)')
                for width, height in ((1280, 720), (1600, 900), (390, 720)):
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
                    if width >= 1280:
                        start = page.locator('#start').bounding_box()
                        self.assertLessEqual(start['y'] + start['height'], height, 'Primary launch visible without scrolling')
                        self.assertLess(page.locator('#github-branch').bounding_box()['y'], 400)
                    self.assertLessEqual(page.locator('#plan-path').bounding_box()['height'], 96)
                    cards = page.locator('#app .card')
                    self.assertEqual(cards.count(), 3)
                    self.assertEqual(page.locator('#app .card .label').all_text_contents(),
                                     ['RUN STATUS', 'COMPLETED TASKS', 'CHILD PROCESS'])
                    positions = [cards.nth(i).bounding_box() for i in range(3)]
                    if width >= 1280:
                        self.assertEqual(len({box['y'] for box in positions}), 1)
                    else:
                        self.assertEqual(len({box['y'] for box in positions}), 3)
                    self.assertEqual(page.locator('#run-setup > :last-child').get_attribute('class'), 'push-policy')
                    self.assertFalse(page.locator('#logs-tools').is_visible())
                # Native keyboard activation reaches the same confirmation boundary.
                page.locator('#start').focus()
                page.keyboard.press('Enter')
                page.wait_for_function("document.getElementById('message').textContent.includes('Cancelled')")
                self.assertFalse(page.evaluate('fixture.controller.active'))
                self.assertEqual(page.evaluate("calls.filter(c => c[0] === 'start')"), [['start', 3]])
                self.assertEqual(page.evaluate('document.activeElement.id'), 'start')
                page.keyboard.press('Space')
                page.wait_for_function("calls.filter(c => c[0] === 'start').length === 2 && !document.getElementById('start').disabled")
                self.assertFalse(page.evaluate('fixture.controller.active'))
                self.assertTrue(page.locator('#fetch-github').is_visible())
                page.locator('#fetch-github').click()
                page.locator('#nav-logs').click()
                page.locator('#list-github').click()
                page.locator('#github-repositories').select_option('owner/other')
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
                page.locator('#local-project > summary').click()
                page.locator('#browse-project').click()
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
                    self.assertIn('<img src=x onerror=bad()>', page.locator('#start-reason').inner_text())
                    self.assertEqual(page.locator('#preflight img, #start-reason img').count(), 0)
                for view in ('tasks', 'logs', 'overview'):
                    page.locator('#nav-' + view).click()
                    self.assertEqual(page.locator('#nav-' + view).get_attribute('aria-current'), 'page')
                    self.assertEqual(page.locator('#run-setup').is_visible(), view == 'overview')
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
            result = subprocess.run([node, str(fixture), str(js)], capture_output=True,
                                    text=True, encoding="utf-8", errors="replace", timeout=20)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)


if __name__ == "__main__":
    unittest.main()
