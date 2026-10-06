"""Topics and tags against real Postgres: the filing system a teacher relies on.

The rules worth proving with a database are the ones a UI cannot check by itself:
a materialised path that stays true after a rename or a move, a hierarchy that cannot
fold into itself, and a label that cannot be removed while questions still carry it.
"""
from __future__ import annotations

import uuid

from helpers import error_of
from sqlalchemy import select

from app.core.database import SessionLocal
from app.models.ops import AuditLog


def mc_body(prompt: str = "Which word means 'kite'?", **over) -> dict:
    body = {
        "type": "multiple_choice",
        "prompt": prompt,
        "config": {
            "options": [
                {"text": "apple"},
                {"text": "kite", "correct": True},
                {"text": "table"},
            ]
        },
        "level": "A2",
    }
    body.update(over)
    return body


async def _topic(client, name: str, parent_id: str | None = None, **over) -> dict:
    resp = await client.post("/api/v1/topics", json={"name": name, "parent_id": parent_id, **over})
    assert resp.status_code == 201, resp.text
    return resp.json()


async def _tag(client, name: str, **over) -> dict:
    resp = await client.post("/api/v1/tags", json={"name": name, **over})
    assert resp.status_code == 201, resp.text
    return resp.json()


async def _tree(client) -> list[dict]:
    resp = await client.get("/api/v1/topics")
    assert resp.status_code == 200, resp.text
    return resp.json()["items"]


def _walk(nodes: list[dict], name: str) -> dict | None:
    for node in nodes:
        if node["name"] == name:
            return node
        found = _walk(node["children"], name)
        if found is not None:
            return found
    return None


def _find(nodes: list[dict], name: str) -> dict:
    node = _walk(nodes, name)
    if node is None:
        raise AssertionError(f"{name!r} is not in the tree: {[n['level_path'] for n in nodes]}")
    return node


async def _question_ids(client) -> list[str]:
    return [item["id"] for item in (await client.get("/api/v1/questions", params={"view": "all"})).json()["items"]]


async def _new_question(client, *, topic_ids: list[str] = (), tag_ids: list[str] = ()) -> str:
    """A ready question filed under the given topics/tags."""
    created = await client.post("/api/v1/questions", json=mc_body(topic_ids=list(topic_ids), tag_ids=list(tag_ids)))
    assert created.status_code == 201, created.text
    return created.json()["id"]


# --------------------------------------------------------------------------- #
# Topics: the tree
# --------------------------------------------------------------------------- #


async def test_the_tree_is_returned_with_paths_and_counts(client):
    grammar = await _topic(client, "Grammar", language="en")
    tenses = await _topic(client, "Tenses", grammar["id"])
    past = await _topic(client, "Past", tenses["id"])
    # Siblings are independent branches.
    await _topic(client, "Skills")

    assert grammar["parent_id"] is None
    assert grammar["level_path"] == "Grammar"
    assert tenses["level_path"] == "Grammar/Tenses"
    assert past["level_path"] == "Grammar/Tenses/Past"
    assert grammar["language"] == "en"
    assert past["question_count"] == 0

    await _new_question(client, topic_ids=[past["id"]])

    items = await _tree(client)
    assert [node["name"] for node in items] == ["Grammar", "Skills"], "roots are ordered by name"
    node = _find(items, "Grammar")
    assert [child["name"] for child in node["children"]] == ["Tenses"]
    assert _find(items, "Past")["question_count"] == 1
    assert node["question_count"] == 0, "a parent counts its own labels, not its subtree's"
    assert (await client.get("/api/v1/topics")).json()["total"] == 4


async def test_a_question_can_carry_several_topics_and_each_counts_it(client):
    first = await _topic(client, "Travel")
    second = await _topic(client, "Past tenses")
    question_id = await _new_question(client, topic_ids=[first["id"]])
    assigned = await client.post(
        f"/api/v1/questions/{question_id}/taxonomy", json={"topic_ids": [first["id"], second["id"]]}
    )
    assert assigned.status_code == 200, assigned.text
    assert sorted(t["name"] for t in assigned.json()["topics"]) == ["Past tenses", "Travel"]

    items = await _tree(client)
    assert _find(items, "Travel")["question_count"] == 1
    assert _find(items, "Past tenses")["question_count"] == 1

    removed = await client.post(f"/api/v1/questions/{question_id}/taxonomy", json={"topic_ids": [second["id"]]})
    assert [t["name"] for t in removed.json()["topics"]] == ["Past tenses"]
    items = await _tree(client)
    assert _find(items, "Travel")["question_count"] == 0


async def test_duplicate_names_are_refused_only_under_the_same_parent(client):
    grammar = await _topic(client, "Grammar")
    clash = await client.post("/api/v1/topics", json={"name": "grammar", "parent_id": grammar["id"]})
    assert clash.status_code == 201, "the same word in a different branch is a different topic"

    await _topic(client, "Tenses", grammar["id"])
    sibling = await client.post("/api/v1/topics", json={"name": " TENSES ", "parent_id": grammar["id"]})
    assert sibling.status_code == 409, sibling.text
    assert error_of(sibling)["code"] == "topic_exists"
    assert (await client.get("/api/v1/topics")).json()["total"] == 3, "a refused create must not add a row"

    root_clash = await client.post("/api/v1/topics", json={"name": "GRAMMAR"})
    assert root_clash.status_code == 409


async def test_a_topic_needs_a_name_and_a_real_parent(client):
    for name in ("", "   ", "\t"):
        resp = await client.post("/api/v1/topics", json={"name": name})
        assert resp.status_code == 422, repr(name)
        assert error_of(resp)["code"] == "validation_failed"

    invented = await client.post("/api/v1/topics", json={"name": "Orphan", "parent_id": str(uuid.uuid4())})
    assert invented.status_code == 422
    assert "parent topic" in error_of(invented)["message"]


async def test_renaming_a_topic_refreshes_every_path_below_it(client):
    root = await _topic(client, "Grammar")
    child = await _topic(client, "Tenses", root["id"])
    grandchild = await _topic(client, "Past", child["id"])

    renamed = await client.patch(f"/api/v1/topics/{root['id']}", json={"name": "Grammatika"})
    assert renamed.status_code == 200, renamed.text
    assert renamed.json()["level_path"] == "Grammatika"

    items = await _tree(client)
    assert _find(items, "Tenses")["level_path"] == "Grammatika/Tenses"
    assert _find(items, "Past")["level_path"] == "Grammatika/Tenses/Past"
    assert _find(items, "Past")["id"] == grandchild["id"], "the children are the same rows, not copies"


async def test_moving_a_topic_carries_its_whole_subtree(client):
    grammar = await _topic(client, "Grammar")
    tenses = await _topic(client, "Tenses", grammar["id"])
    await _topic(client, "Past", tenses["id"])
    skills = await _topic(client, "Skills")

    moved = await client.patch(f"/api/v1/topics/{tenses['id']}", json={"parent_id": skills["id"]})
    assert moved.status_code == 200, moved.text

    items = await _tree(client)
    assert _find(items, "Tenses")["level_path"] == "Skills/Tenses"
    assert _find(items, "Past")["level_path"] == "Skills/Tenses/Past"
    assert _find(items, "Tenses")["children"][0]["name"] == "Past"
    assert _find(items, "Grammar")["children"] == []


async def test_a_topic_cannot_be_moved_inside_itself(client):
    root = await _topic(client, "Root")
    child = await _topic(client, "Child", root["id"])
    grandchild = await _topic(client, "Grandchild", child["id"])

    self_parent = await client.patch(f"/api/v1/topics/{root['id']}", json={"parent_id": root["id"]})
    assert self_parent.status_code == 422
    assert "own parent" in error_of(self_parent)["message"]

    direct_cycle = await client.patch(f"/api/v1/topics/{root['id']}", json={"parent_id": child["id"]})
    assert direct_cycle.status_code == 422
    assert "inside itself" in error_of(direct_cycle)["message"]

    # The cycle is refused however deep the new parent sits below the moved topic.
    deep_cycle = await client.patch(f"/api/v1/topics/{root['id']}", json={"parent_id": grandchild["id"]})
    assert deep_cycle.status_code == 422
    assert "inside itself" in error_of(deep_cycle)["message"]

    # Nothing about the tree changed while it was being refused.
    items = await _tree(client)
    assert _find(items, "Root")["parent_id"] is None
    assert _find(items, "Grandchild")["level_path"] == "Root/Child/Grandchild"

    invented = await client.patch(f"/api/v1/topics/{root['id']}", json={"parent_id": str(uuid.uuid4())})
    assert invented.status_code == 422
    assert _find(await _tree(client), "Root")["parent_id"] is None


async def test_making_a_topic_a_root_rewrites_its_path(client):
    root = await _topic(client, "Grammar")
    child = await _topic(client, "Tenses", root["id"])
    moved = await client.patch(f"/api/v1/topics/{child['id']}", json={"parent_id": None})
    assert moved.json()["parent_id"] is None
    assert moved.json()["level_path"] == "Tenses"
    assert _find(await _tree(client), "Tenses")["level_path"] == "Tenses"


async def test_a_rename_or_a_move_cannot_collide_with_a_sibling(client):
    """Create is not the only way two siblings can end up with one name."""
    grammar = await _topic(client, "Grammar")
    await _topic(client, "Tenses", grammar["id"])
    other = await _topic(client, "Skills")
    verbs = await _topic(client, "Verb tenses", grammar["id"])
    lonely = await _topic(client, "Tenses", other["id"])

    # A move that lands next to a sibling already carrying the name.
    moved = await client.patch(f"/api/v1/topics/{lonely['id']}", json={"parent_id": grammar["id"]})
    assert moved.status_code == 409, moved.text
    assert error_of(moved)["code"] == "topic_exists"

    # A rename that takes a sibling's name, case-insensitively.
    renamed = await client.patch(f"/api/v1/topics/{verbs['id']}", json={"name": "TENSES"})
    assert renamed.status_code == 409, renamed.text

    # Both at once, which is how one combined edit arrives.
    both = await client.patch(f"/api/v1/topics/{verbs['id']}", json={"name": "tenses", "parent_id": other["id"]})
    assert both.status_code == 409, both.text

    items = await _tree(client)
    assert [child["name"] for child in _find(items, "Grammar")["children"]] == ["Tenses", "Verb tenses"]
    assert _find(items, "Skills")["children"][0]["level_path"] == "Skills/Tenses", "a refused patch changed nothing"
    assert _find(items, "Verb tenses")["level_path"] == "Grammar/Verb tenses"

    # Re-saving a topic without changing anything is not a collision with itself.
    same = await client.patch(f"/api/v1/topics/{lonely['id']}", json={"name": "Tenses", "parent_id": other["id"]})
    assert same.status_code == 200, same.text


async def test_a_topic_cannot_be_deleted_while_it_has_children(client):
    root = await _topic(client, "Grammar")
    await _topic(client, "Tenses", root["id"])

    refused = await client.delete(f"/api/v1/topics/{root['id']}")
    assert refused.status_code == 409
    assert error_of(refused)["code"] == "topic_in_use"
    assert "1 subtopic" in error_of(refused)["message"]
    assert _find(await _tree(client), "Grammar")["id"] == root["id"]


async def test_a_topic_cannot_be_deleted_while_a_question_carries_it(client):
    topic = await _topic(client, "Travel")
    question_id = await _new_question(client, topic_ids=[topic["id"]])

    refused = await client.delete(f"/api/v1/topics/{topic['id']}")
    assert refused.status_code == 409
    assert "still labels 1 question" in error_of(refused)["message"]

    # A trashed question still carries the topic: restoring it must not lose its filing.
    await client.delete(f"/api/v1/questions/{question_id}")
    still_used = await client.delete(f"/api/v1/topics/{topic['id']}")
    assert still_used.status_code == 409

    restored = await client.post(f"/api/v1/questions/{question_id}/restore")
    assert [t["name"] for t in restored.json()["topics"]] == ["Travel"]

    await client.post(f"/api/v1/questions/{question_id}/taxonomy", json={"topic_ids": []})
    deleted = await client.delete(f"/api/v1/topics/{topic['id']}")
    assert deleted.status_code == 200, deleted.text
    assert deleted.json() == {"ok": True, "deleted": topic["id"]}
    assert await _tree(client) == []


async def test_unknown_and_malformed_topic_ids_are_not_500(client):
    missing = str(uuid.uuid4())
    assert (await client.patch(f"/api/v1/topics/{missing}", json={"name": "x"})).status_code == 404
    assert (await client.delete(f"/api/v1/topics/{missing}")).status_code == 404
    assert (await client.patch("/api/v1/topics/not-a-uuid", json={"name": "x"})).status_code == 422
    assert (await client.delete("/api/v1/topics/not-a-uuid")).status_code == 422


async def test_topic_writes_are_audited(client):
    created = await _topic(client, "Grammar")
    await client.patch(f"/api/v1/topics/{created['id']}", json={"name": "Grammatika"})
    await client.delete(f"/api/v1/topics/{created['id']}")

    async with SessionLocal() as db:
        rows = (
            (
                await db.execute(
                    select(AuditLog)
                    .where(AuditLog.target_type == "topic", AuditLog.target_id == created["id"])
                    .order_by(AuditLog.action)
                )
            )
            .scalars()
            .all()
        )
    assert [row.action for row in rows] == ["topic.created", "topic.deleted", "topic.updated"]
    assert all(row.actor_type == "admin" and row.actor_id for row in rows)
    by_action = {row.action: row for row in rows}
    assert by_action["topic.created"].after == {"name": "Grammar"}
    assert by_action["topic.updated"].after["level_path"] == "Grammatika"
    assert by_action["topic.deleted"].before == {"name": "Grammatika"}


# --------------------------------------------------------------------------- #
# Tags
# --------------------------------------------------------------------------- #


async def test_tags_are_flat_unique_and_counted(client):
    first = await _tag(client, "exam-style", color="amber")
    second = await _tag(client, "Audio")
    assert first["question_count"] == 0
    assert first["color"] == "amber"
    assert second["name"] == "Audio"

    duplicate = await client.post("/api/v1/tags", json={"name": "EXAM-STYLE"})
    assert duplicate.status_code == 409
    assert error_of(duplicate)["code"] == "tag_exists"
    assert "exam-style" in error_of(duplicate)["message"], "the message must name the tag that already exists"

    body = await client.get("/api/v1/tags")
    assert body.json()["total"] == 2
    # Listing is case-insensitive-sorted so the picker reads A-Z regardless of typing.
    assert [item["name"] for item in body.json()["items"]] == ["Audio", "exam-style"]

    question_id = await _new_question(client, tag_ids=[first["id"]])
    listed = (await client.get("/api/v1/tags")).json()["items"]
    assert {item["name"]: item["question_count"] for item in listed} == {"Audio": 0, "exam-style": 1}

    await client.post(f"/api/v1/questions/{question_id}/taxonomy", json={"tag_ids": [first["id"], second["id"]]})
    listed = (await client.get("/api/v1/tags")).json()["items"]
    assert {item["name"]: item["question_count"] for item in listed} == {"Audio": 1, "exam-style": 1}


async def test_a_tag_needs_a_name(client):
    for name in ("", "  "):
        resp = await client.post("/api/v1/tags", json={"name": name})
        assert resp.status_code == 422, repr(name)
    too_long = await client.post("/api/v1/tags", json={"name": "x" * 121})
    assert too_long.status_code == 422


async def test_renaming_a_tag_cannot_collide_with_another(client):
    kept = await _tag(client, "past")
    other = await _tag(client, "Past tense")

    clash = await client.patch(f"/api/v1/tags/{other['id']}", json={"name": "PAST"})
    assert clash.status_code == 409
    assert error_of(clash)["code"] == "tag_exists"
    assert (await client.get("/api/v1/tags")).json()["total"] == 2

    renamed = await client.patch(f"/api/v1/tags/{other['id']}", json={"name": "irregular", "color": "blue"})
    assert renamed.status_code == 200, renamed.text
    assert renamed.json() == {"id": other["id"], "name": "irregular", "color": "blue", "question_count": 0}
    # Re-sending a tag's own name is not a collision with itself.
    same = await client.patch(f"/api/v1/tags/{kept['id']}", json={"name": "past"})
    assert same.status_code == 200


async def test_a_tag_cannot_be_deleted_while_a_question_carries_it(client):
    tag = await _tag(client, "phrasal")
    question_id = await _new_question(client, tag_ids=[tag["id"]])
    await client.delete(f"/api/v1/questions/{question_id}")

    refused = await client.delete(f"/api/v1/tags/{tag['id']}")
    assert refused.status_code == 409
    assert error_of(refused)["code"] == "tag_in_use"
    assert "still labels 1 question" in error_of(refused)["message"]

    # The trash is frozen: nothing is re-filed behind a question the teacher can still
    # restore, so the label has to be removed after the question comes back.
    while_trashed = await client.post(f"/api/v1/questions/{question_id}/taxonomy", json={"tag_ids": []})
    assert while_trashed.status_code == 404, while_trashed.text
    await client.post(f"/api/v1/questions/{question_id}/restore")
    await client.post(f"/api/v1/questions/{question_id}/taxonomy", json={"tag_ids": []})
    deleted = await client.delete(f"/api/v1/tags/{tag['id']}")
    assert deleted.json() == {"ok": True, "deleted": tag["id"]}
    assert (await client.get("/api/v1/tags")).json()["items"] == []


async def test_unknown_tag_ids_behave_like_unknown_topic_ids(client):
    missing = str(uuid.uuid4())
    assert (await client.patch(f"/api/v1/tags/{missing}", json={"name": "x"})).status_code == 404
    assert (await client.delete(f"/api/v1/tags/{missing}")).status_code == 404
    assert (await client.post("/api/v1/questions", json=mc_body(tag_ids=[missing]))).status_code == 422
    assert await _question_ids(client) == [], "a refused create must not leave a question behind"


async def test_taxonomy_writes_need_a_session_and_the_csrf_header(client, session_factory):
    anonymous = session_factory()._c
    assert (await anonymous.post("/api/v1/topics", json={"name": "Sneaky"})).status_code == 401
    assert (await anonymous.post("/api/v1/tags", json={"name": "Sneaky"})).status_code == 401

    without_header = await client._c.post("/api/v1/topics", json={"name": "Sneaky"})
    assert without_header.status_code == 403
    assert error_of(without_header)["code"] == "csrf_failed"
    assert (await client.get("/api/v1/topics")).json()["items"] == []
