"""Tier-0 deterministic recipes (doc 07 §2): 0 model calls for everyday commands."""
from __future__ import annotations

from pathlib import Path

from eva.config import get_settings
from eva.tasks.recipes import direct_reply, match_recipe

S = get_settings()


def _steps(task):
    return [s.tool for s in task.plan]


def test_open_app_recipes_english_and_roman_urdu():
    for text in ("open chrome", "chrome kholo", "launch chrome please"):
        t = match_recipe(text, S)
        assert t is not None, text
        assert _steps(t) == ["apps.launch"]
        assert t.plan[0].args["name"] == "chrome"
        assert t.planned_by == "recipe"


def test_close_and_focus_recipes():
    t = match_recipe("band karo notepad", S)
    assert _steps(t) == ["apps.close"]
    t2 = match_recipe("focus notepad", S)
    assert _steps(t2) == ["os.focus_window"]


def test_system_report_recipes():
    for text in ("cpu usage", "kya ram usage hai", "disk status batao"):
        t = match_recipe(text, S)
        assert t is not None and _steps(t) == ["os.get_resources"], text


def test_list_windows_recipe():
    t = match_recipe("list windows", S)
    assert _steps(t) == ["os.list_windows"]


def test_whatsapp_flagship_recipe():
    t = match_recipe("open whatsapp in aiou profile, saad bhai ka archived chat kholo", S)
    assert t is not None
    assert _steps(t) == ["chrome.open_profile", "wa.open_archived", "wa.search_contact",
                         "wa.open_chat"]
    assert t.plan[0].args["profile"] == "aiou"
    assert t.plan[2].args["q"] == "Saad Bhai"
    described = t.completion.describe()
    assert "wa.selected_chat" in described and "archived" in described


def test_web_app_vocabulary_and_url_are_data_driven(monkeypatch):
    """A brand-new web app added purely as data (no code change) is understood,
    opened at its own URL, and gets no chat steps unless it declares that
    capability."""
    from eva.apps import registry
    monkeypatch.setattr(registry, "_load_webapp_overrides",
                        lambda: {"myportal": {"url": "https://portal.example.com/",
                                              "aliases": ["myportal", "portal"]}})
    t = match_recipe("open my portal", S)
    assert t is not None
    assert [s.tool for s in t.plan] == ["chrome.open_profile"]
    assert t.plan[0].args["url"] == "https://portal.example.com/"
    assert t.plan[0].args["profile"] == ""
    assert "wa.login" not in t.completion.describe()


def test_whatsapp_url_comes_from_registry_table():
    from eva.apps.registry import find_web_app
    spec = find_web_app("whatsapp")
    assert spec is not None and spec.get("url")
    t = match_recipe("open whatsapp", S)
    assert t.plan[0].args["url"] == spec["url"]
    assert "chat" in spec.get("capabilities", [])


def test_whatsapp_profile_is_generic_not_hardcoded():
    """Any "<X> profile" works; the query is resolved at runtime against the
    machine's real Chrome profiles (l3_cdp.match_profile), not baked in."""
    t = match_recipe("open whatsapp in noah profile", S)
    assert t.plan[0].args["profile"] == "noah"
    t2 = match_recipe("whatsapp kholo in profile 9, archived kholo", S)
    assert t2.plan[0].args["profile"] == "9"


def test_whatsapp_self_profile_means_no_query():
    t = match_recipe("open whatsapp in my profile", S)
    assert t.plan[0].args["profile"] == ""


def test_whatsapp_without_contact_is_just_open():
    t = match_recipe("open whatsapp web", S)
    assert _steps(t) == ["chrome.open_profile"]
    assert "wa.selected_chat" not in t.completion.describe()


def test_create_folder_recipe():
    t = match_recipe('create folder "AIOU Reports" on desktop', S)
    assert _steps(t) == ["files.mkdir"]
    assert t.plan[0].args["path"].replace("\\", "/").endswith("Desktop/AIOU Reports")


def test_run_command_recipe():
    t = match_recipe("run git status", S)
    assert _steps(t) == ["terminal.run"]
    assert t.plan[0].args["command"] == "git status"


def test_reminder_recipe():
    t = match_recipe("remind me in 20 minutes to check the oven", S)
    assert _steps(t) == ["scheduler.add_reminder"]
    assert int(t.plan[0].args["minutes"]) == 20
    t2 = match_recipe("mujhe 2 hours baad pani peene ki yaad dilana", S)
    assert t2 is not None and int(t2.plan[0].args["minutes"]) == 120


def test_organize_downloads_recipe():
    t = match_recipe("organize my downloads", S)
    assert _steps(t) == ["files.organize"]


def test_url_recipe():
    t = match_recipe("open youtube.com", S)
    assert _steps(t) == ["chrome.open_profile"]
    assert t.plan[0].args["url"] == "https://youtube.com"


def test_direct_replies_are_not_tasks():
    assert match_recipe("hello eva", S) is None
    assert direct_reply("hi") and "task" in direct_reply("hi").lower()
    assert direct_reply("help") is not None
    assert direct_reply("open chrome") is None
