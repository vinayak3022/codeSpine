from click.testing import CliRunner

from codespine.cli import main
from codespine.tasks import active_tasks, create_task, finish_task, list_tasks, update_task


def test_task_registry_tracks_running_and_finished_tasks():
    task_id = create_task("enrichment", "Test enrichment", path="/tmp/project")
    update_task(task_id, status="running", phase="community detection", pid=None)

    active = active_tasks()
    assert len(active) == 1
    assert active[0]["id"] == task_id
    assert active[0]["phase"] == "community detection"

    finish_task(task_id, "succeeded", "done")

    assert active_tasks() == []
    recent = list_tasks(include_finished=True)
    assert recent[0]["id"] == task_id
    assert recent[0]["status"] == "succeeded"
    assert recent[0]["result_status"] == "succeeded"
    assert recent[0]["last_phase"] == "community detection"
    assert recent[0]["progress"] == 1.0
    assert recent[0]["detail"] == "done"


def test_background_command_shows_progress():
    task_id = create_task("enrichment", "Test enrichment", path="/tmp/project")
    update_task(task_id, status="running", phase="execution flows", pid=None, progress=0.4)

    result = CliRunner().invoke(main, ["background"])

    assert result.exit_code == 0
    assert task_id in result.output
    assert "40%" in result.output
    assert "execution flows" in result.output


def test_background_command_shows_recent_finished_tasks_by_default():
    task_id = create_task("indexing", "Background core indexing", path="/tmp/project")
    finish_task(task_id, "failed", "boom")

    result = CliRunner().invoke(main, ["background"])

    assert result.exit_code == 0
    assert task_id in result.output
    assert "failed" in result.output
    assert "boom" in result.output


def test_failed_task_preserves_last_phase_and_hint():
    task_id = create_task("repair", "Repair", path="/tmp/project", repair_hint="codespine repair /tmp/project")
    update_task(task_id, status="running", phase="dead code", pid=None, progress=0.75)
    finish_task(task_id, "failed", "parser blew up", repair_hint="codespine repair /tmp/project")

    recent = list_tasks(include_finished=True)

    assert recent[0]["last_phase"] == "dead code"
    assert recent[0]["result_status"] == "failed"
    assert recent[0]["repair_hint"] == "codespine repair /tmp/project"
