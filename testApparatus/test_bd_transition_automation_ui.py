"""Test biodynamic-transition automation wiring in generated UI assets.

These source-level checks also keep the canonical package version marker and
runtime app binding aligned with the UI behavior they support.
"""

from pathlib import Path
import re


def test_repository_version_is_stored_once_and_reexported():
    repo_root = Path(__file__).resolve().parents[1]
    root_source = (repo_root / "__init__.py").read_text(encoding="utf-8")
    package_source = (repo_root / "sensorius" / "__init__.py").read_text(
        encoding="utf-8"
    )

    assert re.search(r'^__version__\s*=\s*"[^"]+"', root_source, re.MULTILINE) is None
    assert re.search(r'^__version__\s*=\s*"[^"]+"', package_source, re.MULTILINE)
    assert "from .sensorius import __version__" in root_source
    assert "from sensorius import __version__" in root_source


def test_web_routes_binds_the_runtime_app_for_background_broadcasts():
    repo_root = Path(__file__).resolve().parents[1]
    routes_text = (
        repo_root / "sensorius" / "saiWebRoutes.py"
    ).read_text(encoding="utf-8")

    assert 'globals()["app"] = app' in routes_text


def test_bd_transition_condition_is_optionless_and_serialized():
    repo_root = Path(__file__).resolve().parents[1]
    js_text = (
        repo_root / "ui_static" / "js" / "advanced_automation.js"
    ).read_text(encoding="utf-8")

    assert '<option value="bd_transitions">BD Transitions</option>' in js_text
    assert 'row.append(typeWrap, rem);' in js_text
    assert 'return { type:"bd_transitions", executor_switch_id: currentSwitchId };' in js_text
    assert 'opt.value = "none";' in js_text
    assert 'opt.textContent = "Alert";' in js_text
    assert 'type: "none"' in js_text
    assert "appendNoneOption();" in js_text


def test_automation_condition_and_action_rows_align_fields_left_and_remove_right():
    repo_root = Path(__file__).resolve().parents[1]

    for css_name in ("app.css", "combined.css"):
        css_text = (repo_root / "ui_static" / "css" / css_name).read_text(encoding="utf-8")

        assert "#pane-automations .section-title," in css_text
        assert "#conditionsContainer,\n#actionsContainer{\n  text-align:left;" in css_text
        assert ".cond.bd-transitions{" in css_text
        assert ".action-row.none-action{" in css_text
        assert ".cond > .remove,\n.action-row > .remove{\n  grid-column:-2 / -1;" in css_text


def test_switch_action_controls_use_operator_facing_restore_labels():
    repo_root = Path(__file__).resolve().parents[1]
    js_text = (
        repo_root / "ui_static" / "js" / "advanced_automation.js"
    ).read_text(encoding="utf-8")

    assert 'setLab.textContent = "Set State";' in js_text
    assert 'revertLab.textContent = "Restore Action";' in js_text
    assert '<option value="previous_state">To previous state</option>' in js_text
    assert '<option value="do_nothing">Leave at set state</option>' in js_text
    assert 'delayLab.textContent = "Delay Action (secs)";' in js_text


def test_none_actor_is_available_for_astral_and_other_conditions():
    repo_root = Path(__file__).resolve().parents[1]
    js_text = (
        repo_root / "ui_static" / "js" / "advanced_automation.js"
    ).read_text(encoding="utf-8")

    option_block = js_text[js_text.index("const appendNoneOption"):js_text.index("function refreshActionActorOptions")]
    assert 'opt.value = "none";' in option_block
    assert "if (!hasBdTransitionCondition(modal))" in option_block


def test_bd_transition_toast_is_persistent_and_shows_from_to():
    repo_root = Path(__file__).resolve().parents[1]
    html_builder = (
        repo_root / "sensorius" / "saiHtml.py"
    ).read_text(encoding="utf-8")

    assert "msg.type === 'bd_transition'" in html_builder
    assert "msg.test ? 'BD Transition Test' : 'BD Transition'" in html_builder
    assert "From ${segmentText(msg.from)} → To ${segmentText(msg.to)}" in html_builder
    assert "window.biodynamicActionColors = biodynamicActionColors;" in html_builder
    assert "const colorHelper = window.biodynamicActionColors;" in html_builder
    assert "typeof colorHelper === 'function'" in html_builder
    assert "t.style.backgroundColor = actionColors.background;" in html_builder
    assert "t.style.color = actionColors.text;" in html_builder
    assert "const background = isFruit ? '#4c3a7f'" in html_builder
    assert "const actionColors = biodynamicActionColors(cur);" in html_builder
    assert "openBtn.style.background = actionColors.background;" in html_builder
    assert "openBtn.style.color = actionColors.text;" in html_builder
    assert "Click to dismiss" in html_builder
    assert "setTimeout" not in "\n".join(
        line
        for line in html_builder.splitlines()
        if "bd-transition-toast" in line
    )


def test_bd_transition_test_endpoint_uses_live_dashboard_broadcaster():
    repo_root = Path(__file__).resolve().parents[1]
    routes_text = (
        repo_root / "sensorius" / "saiWebRoutes.py"
    ).read_text(encoding="utf-8")

    assert '@router.post("/advanced/automations/test-bd-transition"' in routes_text
    assert '"test": True' in routes_text
    assert "await broadcaster(payload)" in routes_text


def test_generic_automation_toast_is_persistent_and_click_dismissible():
    repo_root = Path(__file__).resolve().parents[1]
    html_builder = (
        repo_root / "sensorius" / "saiHtml.py"
    ).read_text(encoding="utf-8")
    branch = html_builder[
        html_builder.index("msg.type === 'automation_notification'"):
        html_builder.index("msg.type === 'bd_transition'")
    ]

    assert "automation-notification-toast" in branch
    assert "details.join('; ')" in branch
    assert "Click to dismiss" in branch
    assert "addEventListener('click'" in branch
    assert "setTimeout" not in branch
    assert (
        ".toast.automation-notification-toast{background:#a34700;color:#fff;cursor:pointer}"
        in html_builder
    )


def test_email_failure_toast_is_persistent_error_and_click_dismissible():
    repo_root = Path(__file__).resolve().parents[1]
    html_builder = (repo_root / "sensorius" / "saiHtml.py").read_text(encoding="utf-8")
    branch = html_builder[
        html_builder.index("msg.type === 'email_failure'"):
        html_builder.index("msg.type === 'bd_transition'")
    ]

    assert "Email delivery failed after ${attempts}" in branch
    assert "toast error email-failure-toast" in branch
    assert "Click to dismiss" in branch
    assert "addEventListener('click'" in branch
    assert "setTimeout" not in branch
