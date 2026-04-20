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
    assert recent[0]["detail"] == "done"


def test_background_command_shows_progress():
    task_id = create_task("enrichment", "Test enrichment", path="/tmp/project")
    update_task(task_id, status="running", phase="execution flows", pid=None, progress=0.4)

    result = CliRunner().invoke(main, ["background"])

    assert result.exit_code == 0
    assert task_id in result.output
    assert "40%" in result.output
    assert "execution flows" in result.output
