from voice_to_note.transforms.todos import (
    StoredTodo,
    TodoItem,
    normalize,
    reconcile,
    todo_items,
)


def item(text: str, owner: str = "", deadline: str = "") -> TodoItem:
    """One to-do as a fresh extraction states it."""
    return TodoItem(text, normalize(text), owner, deadline)


def stored(todo_id: int, text: str, *, touched: bool = False) -> StoredTodo:
    """One to-do already on the memo, as planning reads it."""
    return StoredTodo(todo_id, normalize(text), touched)


# --- recognising one task as another --------------------------------------


def test_the_same_task_written_differently_comes_to_the_same_key():
    # the model restates commitments in its own words every run; only the
    # spacing, the case and the full stop are allowed to differ
    assert normalize("  Cut   the RELEASE. ") == normalize("cut the release")


def test_an_extraction_that_repeats_itself_leaves_one_thing_to_do():
    items = todo_items(
        [
            {"task": "Cut the release", "owner": "Alice", "deadline": None},
            {"task": "cut the release.", "owner": None, "deadline": None},
            {"task": "  ", "owner": None, "deadline": None},
        ]
    )

    assert [(i.text, i.owner) for i in items] == [("Cut the release", "Alice")]


# --- reconciling a memo's list with a fresh extraction ---------------------


def test_a_task_the_new_notes_reword_keeps_its_row_and_is_restated():
    # the row is what carries whether it is done; a new row for the same task
    # would lose that and leave the same thing to check off twice
    plan = reconcile([item("Cut the release.")], [stored(7, "cut the release")], set())

    assert plan.delete == []
    assert plan.insert == []
    assert [(todo_id, i.text) for todo_id, i in plan.refresh] == [(7, "Cut the release.")]


def test_a_task_somebody_has_had_their_say_about_is_left_exactly_as_it_is():
    # restating a touched row in a later run's words would leave it saying
    # something other than what its owner acted on
    plan = reconcile(
        [item("Cut the release.")], [stored(7, "cut the release", touched=True)], set()
    )

    assert (plan.insert, plan.refresh, plan.delete) == ([], [], [])


def test_a_task_the_new_notes_forgot_survives_once_it_has_been_touched():
    # checking something off is the user's own work, and a rerun of the
    # extraction must not throw it away
    plan = reconcile([], [stored(7, "Cut the release", touched=True)], set())

    assert plan.delete == []


def test_a_task_nobody_touched_and_the_new_notes_forgot_is_dropped():
    # an untouched row is the model's own noise, and a better run corrects it
    plan = reconcile([], [stored(7, "Cut the release")], set())

    assert plan.delete == [7]


def test_a_task_already_open_elsewhere_in_the_project_is_not_taken_on_twice():
    # the same commitment restated in next week's standup is the one already
    # open — unless it was done, in which case it has come round again
    fresh = [item("Cut the release")]

    assert reconcile(fresh, [], {normalize("cut the release")}).insert == []
    assert [i.text for i in reconcile(fresh, [], set()).insert] == ["Cut the release"]
