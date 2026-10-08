"""Catalogs over HTTP, against real Postgres (Phase 6).

The offline rules (`tests/test_catalog_rules.py`) settle the words and the descriptions.
What only a live database can prove is the behaviour of a list of references:

* a catalog is a *reference*, so removing one leaves the content in its own bank, and no
  question version is written by any of it - grouping is not a content edit;
* the unique index behind "one catalog names one piece of content once" really holds when
  two teachers write at the same moment;
* availability is a chain, so publishing, trashing and restoring a folder changes what a
  learner can reach;
* a reference whose block has since been deleted is shown as broken rather than hidden;
* every refusal is checked against the table, because a 422 that still wrote a row looks
  like a clean refusal from the outside.
"""
from __future__ import annotations

import asyncio
import uuid

from helpers import error_of
from sqlalchemy import func, select

import app.models  # noqa: F401  - registers every table the truncating fixtures clean
from app.core.database import SessionLocal
from app.models.activity import ActivityEvent
from app.models.assessment import Catalog, CatalogItem
from app.models.content import Question, QuestionVersion
from app.models.ops import AuditLog

CATALOGS = "/api/v1/catalogs"
QUESTIONS = "/api/v1/questions"
VOCABULARY = "/api/v1/vocabulary"
READING = "/api/v1/reading"

#: A string that only ever belongs in a teacher-only field. If it appears in a learner
#: payload the test that looked for it fails loudly.
KEY = "ZULU-CATALOG-KEY"


def choice_body(prompt: str = "Which word means 'kite'?", **over) -> dict:
    body = {
        "type": "multiple_choice",
        "prompt": prompt,
        "config": {
            "options": [
                {"text": "apple", "correct": True},
                {"text": "table"},
                {"text": "window"},
            ]
        },
        "explanation": f"{KEY}: an apple is the fruit.",
        "teacher_notes": f"{KEY} again, in the notes only the teacher reads.",
        "score": 2,
        "level": "A2",
        "learning_language": "en",
    }
    body.update(over)
    return body


def word_body(word: str = "improve", **over) -> dict:
    body = {
        "word": word,
        "learning_language": "en",
        "definition": "to make something better",
        "part_of_speech": "verb",
        "level": "B1",
        "notes": f"{KEY}: bring up the comparative.",
        "translations": [{"language": "az", "value": "təkmilləşdirmək"}],
    }
    body.update(over)
    return body


async def _question(client, prompt: str = "Which word means 'kite'?", **over) -> dict:
    resp = await client.post(QUESTIONS, json=choice_body(prompt, **over))
    assert resp.status_code == 201, resp.text
    return resp.json()


async def _word(client, word: str = "improve", **over) -> dict:
    resp = await client.post(VOCABULARY, json=word_body(word, **over))
    assert resp.status_code == 201, resp.text
    return resp.json()


async def _catalog(client, name: str = "Spring practice", **over) -> dict:
    body = {"name": name, "learning_language": "en", "level": "B1"}
    body.update(over)
    resp = await client.post(CATALOGS, json=body)
    assert resp.status_code == 201, resp.text
    return resp.json()


async def _detail(client, catalog_id: str) -> dict:
    resp = await client.get(f"{CATALOGS}/{catalog_id}")
    assert resp.status_code == 200, resp.text
    return resp.json()


async def _add_items(client, catalog_id: str, *items: dict) -> dict:
    resp = await client.post(f"{CATALOGS}/{catalog_id}/items", json={"items": list(items)})
    assert resp.status_code == 201, resp.text
    return resp.json()


async def _reference(kind: str, ref_id: str, **config) -> dict:
    return {"kind": kind, "ref_id": ref_id, "config": config}


async def _rows(*, trashed: bool = False) -> list[Catalog]:
    async with SessionLocal() as db:
        stmt = select(Catalog)
        stmt = stmt.where(Catalog.deleted_at.is_not(None) if trashed else Catalog.deleted_at.is_(None))
        return list((await db.execute(stmt.order_by(Catalog.created_at))).scalars().all())


async def _item_count(catalog_id: str) -> int:
    async with SessionLocal() as db:
        return int(
            (
                await db.execute(
                    select(func.count()).select_from(CatalogItem).where(CatalogItem.catalog_id == uuid.UUID(catalog_id))
                )
            ).scalar_one()
        )


async def _versions() -> int:
    async with SessionLocal() as db:
        return int((await db.execute(select(func.count()).select_from(QuestionVersion))).scalar_one())


# --------------------------------------------------------------------------- #
# Reading a catalog reads through to the banks
# --------------------------------------------------------------------------- #


async def test_a_catalog_holds_references_and_describes_them_from_their_own_rows(client):
    question = await _question(client)
    word = await _word(client)
    catalog = (
        await _add_items(
            client,
            (await _catalog(client))["id"],
            await _reference("question", question["id"]),
            await _reference("vocabulary", word["id"]),
        )
    )["catalog"]

    assert catalog["item_count"] == 2
    assert catalog["counts"] == {"question": 1, "vocabulary": 1}
    assert catalog["unavailable_count"] == 0
    by_kind = {item["kind"]: item for item in catalog["items"]}
    assert by_kind["question"]["title"] == question["prompt"]
    assert by_kind["question"]["detail"] == "multiple_choice · 2"
    assert by_kind["vocabulary"]["title"] == "improve"
    assert by_kind["vocabulary"]["detail"] == "verb"
    assert [item["position"] for item in catalog["items"]] == [0, 1]


async def test_correcting_a_word_in_the_bank_corrects_it_in_every_catalog_that_names_it(client):
    """The reason a catalog is not a copy: there is nothing here to fall out of date."""
    word = await _word(client)
    catalog = await _catalog(client)
    await _add_items(client, catalog["id"], await _reference("vocabulary", word["id"]))

    renamed = await client.patch(f"{VOCABULARY}/{word['id']}", json={"word": "develop"})
    assert renamed.status_code == 200, renamed.text

    read = await _detail(client, catalog["id"])
    assert read["items"][0]["title"] == "develop"
    async with SessionLocal() as db:
        stored = (await db.execute(select(CatalogItem))).scalars().one()
        assert stored.config == {}, "a reference stores nothing but the block it names"


async def test_removing_a_reference_leaves_the_content_in_its_own_bank(client):
    question = await _question(client)
    catalog = await _catalog(client)
    added = await _add_items(client, catalog["id"], await _reference("question", question["id"]))
    item_id = added["catalog"]["items"][0]["id"]
    before = await _versions()

    resp = await client.delete(f"{CATALOGS}/items/{item_id}")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["content_untouched"] is True
    assert body["items_left"] == 0

    async with SessionLocal() as db:
        assert await db.get(Question, uuid.UUID(question["id"])) is not None
        assert await _versions() == before, "grouping is not a content edit"
    assert (await _detail(client, catalog["id"]))["item_count"] == 0


# --------------------------------------------------------------------------- #
# Folders
# --------------------------------------------------------------------------- #


async def test_a_catalog_inside_a_folder_carries_its_breadcrumb(client):
    folder = await _catalog(client, "Unit 1", status="ready")
    child = await _catalog(client, "Week two", parent_id=folder["id"])

    read = await _detail(client, child["id"])
    assert read["parent_id"] == folder["id"]
    assert read["parent_name"] == "Unit 1"
    assert read["path"] == [{"id": folder["id"], "name": "Unit 1"}]
    assert (await _detail(client, folder["id"]))["child_count"] == 1


async def test_two_catalogs_in_one_folder_cannot_share_a_name(client):
    await _catalog(client, "Spring B2")
    clash = await client.post(CATALOGS, json={"name": "  spring b2  ", "learning_language": "en"})
    assert clash.status_code == 409, clash.text
    err = error_of(clash)
    assert err["code"] == "catalog_name_taken"
    assert "already in this folder" in err["message"]
    assert len(await _rows()) == 1

    # The same name one folder over is a different list, and is allowed.
    folder = await _catalog(client, "Unit 1")
    assert (await _catalog(client, "Spring B2", parent_id=folder["id"]))["name"] == "Spring B2"


async def test_a_catalog_cannot_be_put_inside_itself_or_inside_its_own_folder(client):
    folder = await _catalog(client, "Unit 1")
    child = await _catalog(client, "Week two", parent_id=folder["id"])

    into_itself = await client.patch(f"{CATALOGS}/{folder['id']}", json={"parent_id": folder["id"]})
    assert into_itself.status_code == 422, into_itself.text
    assert "cannot be inside itself" in error_of(into_itself)["message"]

    into_its_child = await client.patch(f"{CATALOGS}/{folder['id']}", json={"parent_id": child["id"]})
    assert into_its_child.status_code == 422, into_its_child.text
    assert "inside one of its own folders" in error_of(into_its_child)["message"]

    missing = await client.patch(f"{CATALOGS}/{child['id']}", json={"parent_id": str(uuid.uuid4())})
    assert missing.status_code == 422, missing.text
    assert "not in the bank" in error_of(missing)["message"]


async def test_nesting_stops_at_the_cap_even_when_a_whole_subtree_is_moved(client):
    """The cap is measured on the subtree, not on the one row the request named."""
    deep = await _catalog(client, "Level 1")
    for name in ("Level 2", "Level 3"):
        deep = await _catalog(client, name, parent_id=deep["id"])
    leaf = await _catalog(client, "Level 4", parent_id=deep["id"])

    other_root = await _catalog(client, "Another unit")
    refused = await client.patch(f"{CATALOGS}/{other_root['id']}", json={"parent_id": leaf["id"]})
    assert refused.status_code == 422, refused.text
    assert "nested 4 levels deep at most" in error_of(refused)["message"]
    assert (await _detail(client, other_root["id"]))["parent_id"] is None


async def test_a_folder_can_be_published_with_nothing_in_it_but_a_catalog_cannot(client):
    """Availability is a chain: an unpublished folder hides every lesson inside it."""
    empty = await _catalog(client, "Empty list")
    refused = await client.post(f"{CATALOGS}/{empty['id']}/status", json={"status": "ready"})
    assert refused.status_code == 422, refused.text
    assert "an empty catalog cannot be published" in error_of(refused)["message"]

    folder = await _catalog(client, "Unit 1")
    child = await _catalog(client, "Week two", parent_id=folder["id"])
    await _add_items(client, child["id"], await _reference("question", (await _question(client))["id"]))
    assert (await client.post(f"{CATALOGS}/{child['id']}/status", json={"status": "ready"})).status_code == 200
    published = await client.post(f"{CATALOGS}/{folder['id']}/status", json={"status": "ready"})
    assert published.status_code == 200, published.text


async def test_a_catalog_whose_content_has_all_been_binned_cannot_be_published(client):
    question = await _question(client)
    catalog = await _catalog(client)
    await _add_items(client, catalog["id"], await _reference("question", question["id"]))
    assert (await client.post(f"{CATALOGS}/{catalog['id']}/status", json={"status": "ready"})).status_code == 200

    assert (await client.post(f"{QUESTIONS}/{question['id']}/status", json={"status": "draft"})).status_code == 200
    read = await _detail(client, catalog["id"])
    assert read["items"][0]["state"] == "draft"
    assert read["items"][0]["available_to_learner"] is False
    assert read["unavailable_count"] == 1
    assert read["available_to_learner"] is True, "the catalog is still published; its content is not usable"

    # It stays published - the teacher is shown the hole rather than having the catalog
    # quietly unpublished under them - but it cannot be re-published while it is like this.
    assert (await client.post(f"{CATALOGS}/{catalog['id']}/status", json={"status": "archived"})).status_code == 200
    refused = await client.post(f"{CATALOGS}/{catalog['id']}/status", json={"status": "ready"})
    assert refused.status_code == 422, refused.text
    assert "none of the content in this catalog can be used" in error_of(refused)["message"]


# --------------------------------------------------------------------------- #
# Trash and restore
# --------------------------------------------------------------------------- #


async def test_trashing_a_folder_is_refused_while_live_catalogs_sit_inside_it(client):
    folder = await _catalog(client, "Unit 1")
    child = await _catalog(client, "Week two", parent_id=folder["id"])

    refused = await client.delete(f"{CATALOGS}/{folder['id']}")
    assert refused.status_code == 422, refused.text
    assert "still holds 1 folder" in error_of(refused)["message"]
    assert len(await _rows(trashed=True)) == 0

    assert (await client.delete(f"{CATALOGS}/{child['id']}")).status_code == 200
    assert (await client.delete(f"{CATALOGS}/{folder['id']}")).status_code == 200
    assert len(await _rows(trashed=True)) == 2
    assert len(await _rows()) == 0

    restored = await client.post(f"{CATALOGS}/{child['id']}/restore")
    assert restored.status_code == 200, restored.text
    assert restored.json()["deleted_at"] is None
    # The folder it lives in is still binned, so it stays invisible through the chain.
    assert restored.json()["available_to_learner"] is False


async def test_a_name_taken_while_a_catalog_was_in_the_bin_is_refused_not_renamed(client):
    first = await _catalog(client, "Spring B2")
    assert (await client.delete(f"{CATALOGS}/{first['id']}")).status_code == 200
    await _catalog(client, "Spring B2")

    refused = await client.post(f"{CATALOGS}/{first['id']}/restore")
    assert refused.status_code == 409, refused.text
    assert error_of(refused)["code"] == "catalog_name_taken"
    assert len(await _rows()) == 1
    assert len(await _rows(trashed=True)) == 1


async def test_a_trashed_catalog_cannot_be_edited_or_added_to(client):
    catalog = await _catalog(client)
    question = await _question(client)
    assert (await client.delete(f"{CATALOGS}/{catalog['id']}")).status_code == 200

    refused = await client.patch(f"{CATALOGS}/{catalog['id']}", json={"name": "Renamed"})
    assert refused.status_code == 404, refused.text
    assert error_of(refused)["code"] == "not_found"

    refused_items = await client.post(
        f"{CATALOGS}/{catalog['id']}/items", json={"items": [await _reference("question", question["id"])]}
    )
    assert refused_items.status_code == 404, refused_items.text

    refused_status = await client.post(f"{CATALOGS}/{catalog['id']}/status", json={"status": "ready"})
    assert refused_status.status_code == 422, refused_status.text
    assert "restore the catalog from the trash" in error_of(refused_status)["message"]
    assert await _item_count(catalog["id"]) == 0


# --------------------------------------------------------------------------- #
# References
# --------------------------------------------------------------------------- #


async def test_one_catalog_names_one_piece_of_content_once(client):
    question = await _question(client)
    catalog = await _catalog(client)
    await _add_items(client, catalog["id"], await _reference("question", question["id"]))

    again = await client.post(
        f"{CATALOGS}/{catalog['id']}/items", json={"items": [await _reference("question", question["id"])]}
    )
    assert again.status_code == 409, again.text
    err = error_of(again)
    assert err["code"] == "reference_exists"
    assert question["prompt"] in err["message"], "the refusal names the content the teacher already added"
    assert await _item_count(catalog["id"]) == 1


async def test_two_teachers_adding_the_same_content_at_the_same_moment_leave_one_row(
    client, session_factory
):
    """The unique index, not the pre-check, is what settles a race."""
    question = await _question(client)
    catalog = await _catalog(client)

    second = session_factory()
    await second.login_admin()
    body = {"items": [await _reference("question", question["id"])]}
    first, other = await asyncio.gather(
        client.post(f"{CATALOGS}/{catalog['id']}/items", json=body),
        second.post(f"{CATALOGS}/{catalog['id']}/items", json=body),
    )
    codes = sorted([first.status_code, other.status_code])
    assert codes == [201, 409], f"{first.status_code} {first.text} / {other.status_code} {other.text}"
    refused = first if first.status_code == 409 else other
    assert error_of(refused)["code"] == "reference_exists"
    assert await _item_count(catalog["id"]) == 1


async def test_a_reference_to_content_that_is_gone_or_binned_is_refused(client):
    catalog = await _catalog(client)
    missing = await client.post(
        f"{CATALOGS}/{catalog['id']}/items",
        json={"items": [await _reference("question", str(uuid.uuid4()))]},
    )
    assert missing.status_code == 422, missing.text
    assert "may have been deleted" in error_of(missing)["message"]

    word = await _word(client)
    assert (await client.delete(f"{VOCABULARY}/{word['id']}")).status_code == 200
    binned = await client.post(
        f"{CATALOGS}/{catalog['id']}/items", json={"items": [await _reference("vocabulary", word["id"])]}
    )
    assert binned.status_code == 422, binned.text
    assert "in the trash" in error_of(binned)["message"]
    assert await _item_count(catalog["id"]) == 0


async def test_a_body_that_fails_on_its_last_reference_writes_none_of_them(client):
    """All checked before anything is written: six items and a hole is not a save."""
    first = await _question(client, "First?")
    second = await _question(client, "Second?")
    catalog = await _catalog(client)
    refused = await client.post(
        f"{CATALOGS}/{catalog['id']}/items",
        json={
            "items": [
                await _reference("question", first["id"]),
                await _reference("question", second["id"]),
                await _reference("question", str(uuid.uuid4())),
            ]
        },
    )
    assert refused.status_code == 422, refused.text
    assert await _item_count(catalog["id"]) == 0


async def test_an_unknown_kind_is_refused_by_the_body_before_the_service_sees_it(client):
    catalog = await _catalog(client)
    resp = await client.post(
        f"{CATALOGS}/{catalog['id']}/items",
        json={"items": [{"kind": "exam", "ref_id": str(uuid.uuid4())}]},
    )
    assert resp.status_code == 422, resp.text
    assert "kind must be one of" in error_of(resp)["message"]


async def test_a_question_reference_may_not_name_a_block(client):
    question = await _question(client)
    catalog = await _catalog(client)
    resp = await client.post(
        f"{CATALOGS}/{catalog['id']}/items",
        json={
            "items": [
                {"kind": "question", "ref_id": question["id"], "config": {"set_id": str(uuid.uuid4())}},
            ]
        },
    )
    assert resp.status_code == 422, resp.text
    assert "has no blocks to name" in error_of(resp)["message"]


async def test_a_block_reference_must_be_a_block_of_the_passage_it_names(client):
    reading = await _reading(client)
    other = await _reading(client, title="Another text")
    set_id = (await _set(client, other["id"], "Part one"))["id"]
    catalog = await _catalog(client)

    resp = await client.post(
        f"{CATALOGS}/{catalog['id']}/items",
        json={"items": [await _reference("reading", reading["id"], set_id=set_id)]},
    )
    assert resp.status_code == 422, resp.text
    assert "does not belong to the reading text" in error_of(resp)["message"]
    assert await _item_count(catalog["id"]) == 0


async def test_a_block_reference_that_goes_stale_is_shown_as_broken(client):
    """A hole in a lesson has to be visible, not quietly served as the whole text."""
    reading = await _reading(client)
    question = await _reading_question(client, reading["id"], "First?")
    set_id = (await _set(client, reading["id"], "Part one"))["id"]
    await _file(client, set_id, question["id"])
    catalog = await _catalog(client)
    added = await _add_items(client, catalog["id"], await _reference("reading", reading["id"], set_id=set_id))
    item_id = added["catalog"]["items"][0]["id"]
    assert added["catalog"]["items"][0]["detail"].endswith("Part one")

    assert (await client.delete(f"{READING}/sets/{set_id}")).status_code == 200
    read = await _detail(client, catalog["id"])
    assert read["items"][0]["state"] == "broken_block"
    assert read["items"][0]["available_to_learner"] is False
    assert read["unavailable_count"] == 1

    pointed_elsewhere = await client.patch(f"{CATALOGS}/items/{item_id}", json={"config": {}})
    assert pointed_elsewhere.status_code == 200, pointed_elsewhere.text
    assert pointed_elsewhere.json()["state"] == "ready"


async def test_reordering_needs_every_item_of_the_catalog_exactly_once(client):
    first = await _question(client, "First?")
    second = await _question(client, "Second?")
    catalog = await _catalog(client)
    added = await _add_items(
        client,
        catalog["id"],
        await _reference("question", first["id"]),
        await _reference("question", second["id"]),
    )
    ids = [item["id"] for item in added["catalog"]["items"]]

    partial = await client.post(f"{CATALOGS}/{catalog['id']}/items/reorder", json={"item_ids": ids[:1]})
    assert partial.status_code == 422, partial.text
    assert "(1 missing)" in error_of(partial)["message"]

    stranger = await client.post(
        f"{CATALOGS}/{catalog['id']}/items/reorder", json={"item_ids": [ids[0], str(uuid.uuid4())]}
    )
    assert stranger.status_code == 422, stranger.text
    assert "not in this catalog" in error_of(stranger)["message"]

    reversed_order = await client.post(
        f"{CATALOGS}/{catalog['id']}/items/reorder", json={"item_ids": list(reversed(ids))}
    )
    assert reversed_order.status_code == 200, reversed_order.text
    assert [item["id"] for item in reversed_order.json()["items"]] == list(reversed(ids))
    assert [item["position"] for item in reversed_order.json()["items"]] == [0, 1]


# --------------------------------------------------------------------------- #
# Listing and bulk
# --------------------------------------------------------------------------- #


async def test_the_list_filters_agree_with_the_rows(client):
    question = await _question(client)
    unit = await _catalog(client, "Unit one", level="B1")
    week = await _catalog(client, "Week two", parent_id=unit["id"], level="A2")
    await _add_items(client, week["id"], await _reference("question", question["id"]))
    binned = await _catalog(client, "Old spring list")
    assert (await client.delete(f"{CATALOGS}/{binned['id']}")).status_code == 200

    bank = (await client.get(CATALOGS)).json()
    assert bank["total"] == 2
    assert {row["name"] for row in bank["items"]} == {"Unit one", "Week two"}
    assert all(row["unavailable_count"] is None for row in bank["items"]), (
        "a list row that did not resolve its references must not report a count of zero"
    )

    trash = (await client.get(CATALOGS, params={"view": "trash"})).json()
    assert [row["name"] for row in trash["items"]] == ["Old spring list"]

    everything = (await client.get(CATALOGS, params={"view": "all"})).json()
    assert everything["total"] == 3

    roots = (await client.get(CATALOGS, params={"root_only": True})).json()
    assert [row["name"] for row in roots["items"]] == ["Unit one"]

    inside = (await client.get(CATALOGS, params={"parent_id": unit["id"]})).json()
    assert [row["name"] for row in inside["items"]] == ["Week two"]

    by_kind = (await client.get(CATALOGS, params={"kind": "question"})).json()
    assert [row["name"] for row in by_kind["items"]] == ["Week two"]
    assert (await client.get(CATALOGS, params={"kind": "listening"})).json()["total"] == 0

    found = (await client.get(CATALOGS, params={"q": "week"})).json()
    assert [row["name"] for row in found["items"]] == ["Week two"]
    assert (await client.get(CATALOGS, params={"q": "nothing here"})).json()["total"] == 0

    by_level = (await client.get(CATALOGS, params={"level": "a2"})).json()
    assert [row["name"] for row in by_level["items"]] == ["Week two"]

    unknown_view = await client.get(CATALOGS, params={"view": "archive"})
    assert unknown_view.status_code == 422, unknown_view.text
    assert "view must be one of" in error_of(unknown_view)["message"]


async def test_sorting_and_paging_split_the_same_rows(client):
    for index in range(5):
        await _catalog(client, f"List {index}")
    ascending = (await client.get(CATALOGS, params={"sort": "name", "order": "asc"})).json()
    assert [row["name"] for row in ascending["items"]] == [f"List {index}" for index in range(5)]

    page = (await client.get(CATALOGS, params={"sort": "name", "order": "asc", "page": 2, "page_size": 2})).json()
    assert page["total"] == 5
    assert [row["name"] for row in page["items"]] == ["List 2", "List 3"]
    assert page["page"] == 2 and page["page_size"] == 2


async def test_bulk_answers_for_every_id_it_was_handed(client):
    publishable = await _catalog(client, "Ready to go")
    await _add_items(client, publishable["id"], await _reference("question", (await _question(client))["id"]))
    empty = await _catalog(client, "Empty one")
    missing = str(uuid.uuid4())

    resp = await client.post(
        f"{CATALOGS}/bulk",
        json={"catalog_ids": [publishable["id"], empty["id"], missing], "action": "status", "status": "ready"},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["updated"] == [publishable["id"]]
    assert body["not_found"] == [missing]
    assert len(body["refused"]) == 1
    assert body["refused"][0]["id"] == empty["id"]
    assert "an empty catalog cannot be published" in body["refused"][0]["reason"]


async def test_a_bulk_action_that_carries_a_value_refuses_to_guess_it(client):
    catalog = await _catalog(client, "Levelled", level="B1")
    no_level = await client.post(
        f"{CATALOGS}/bulk", json={"catalog_ids": [catalog["id"]], "action": "set_level"}
    )
    assert no_level.status_code == 422, no_level.text
    assert "choose the level to set" in error_of(no_level)["message"]

    no_status = await client.post(f"{CATALOGS}/bulk", json={"catalog_ids": [catalog["id"]], "action": "status"})
    assert no_status.status_code == 422, no_status.text
    assert "choose the status to set" in error_of(no_status)["message"]
    assert (await _detail(client, catalog["id"]))["level"] == "B1", "a refused bulk must change nothing"

    applied = await client.post(
        f"{CATALOGS}/bulk", json={"catalog_ids": [catalog["id"]], "action": "set_level", "level": "c1"}
    )
    assert applied.status_code == 200, applied.text
    assert (await _detail(client, catalog["id"]))["level"] == "C1"


async def test_bulk_trash_and_restore_answer_per_row(client):
    folder = await _catalog(client, "Unit one")
    child = await _catalog(client, "Week two", parent_id=folder["id"])

    refused = await client.post(f"{CATALOGS}/bulk", json={"catalog_ids": [folder["id"]], "action": "trash"})
    assert refused.json()["updated"] == []
    assert "still holds 1 folder" in refused.json()["refused"][0]["reason"]

    both = await client.post(
        f"{CATALOGS}/bulk", json={"catalog_ids": [child["id"], folder["id"]], "action": "trash"}
    )
    # The child is binned first, so the folder behind it can go too - in one request.
    assert sorted(both.json()["updated"]) == sorted([child["id"], folder["id"]]), both.text
    assert len(await _rows(trashed=True)) == 2

    already = await client.post(f"{CATALOGS}/bulk", json={"catalog_ids": [child["id"]], "action": "trash"})
    assert "already in the trash" in already.json()["refused"][0]["reason"]

    back = await client.post(f"{CATALOGS}/bulk", json={"catalog_ids": [child["id"], folder["id"]], "action": "restore"})
    assert sorted(back.json()["updated"]) == sorted([child["id"], folder["id"]]), back.text
    assert len(await _rows()) == 2


# --------------------------------------------------------------------------- #
# The teacher's preview
# --------------------------------------------------------------------------- #


async def test_a_preview_shows_what_a_learner_would_get_and_writes_no_event(client):
    question = await _question(client)
    draft = await _question(client, "Still being written?")
    assert (await client.post(f"{QUESTIONS}/{draft['id']}/status", json={"status": "draft"})).status_code == 200
    catalog = await _catalog(client)
    await _add_items(
        client,
        catalog["id"],
        await _reference("question", question["id"]),
        await _reference("question", draft["id"]),
    )

    resp = await client.get(f"{CATALOGS}/{catalog['id']}/preview")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["skipped_count"] == 1, "a draft is left out and the teacher is told how many were"
    assert [step["kind"] for step in body["steps"]] == ["question"]
    assert body["steps"][0]["view"]["prompt"] == question["prompt"]
    assert "session_id" not in body, "a preview is not a run and must not issue a token"
    assert KEY not in resp.text, "a preview is served to a teacher, but the key still does not belong in it"

    async with SessionLocal() as db:
        assert int((await db.execute(select(func.count()).select_from(ActivityEvent))).scalar_one()) == 0


# --------------------------------------------------------------------------- #
# Permissions, CSRF and the audit trail
# --------------------------------------------------------------------------- #


async def test_a_learner_cannot_reach_the_catalog_surface_at_all(client, session_factory):
    student = await client.post(
        "/api/v1/students", json={"name": "Learner", "surname": "Card", "username": "catalog-no-access"}
    )
    assert student.status_code == 201, student.text
    learner = session_factory()
    await learner.login_student(student.json()["access_key"])

    catalog = await _catalog(client)
    routes = (
        ("get", CATALOGS, None),
        ("get", f"{CATALOGS}/{catalog['id']}", None),
        ("get", f"{CATALOGS}/{catalog['id']}/preview", None),
        ("post", CATALOGS, {"name": "Intruder"}),
        ("post", f"{CATALOGS}/bulk", {"catalog_ids": [catalog["id"]], "action": "trash"}),
        ("delete", f"{CATALOGS}/{catalog['id']}", None),
    )
    for method, path, body in routes:
        if body is None:
            resp = await getattr(learner, method)(path)
        else:
            resp = await getattr(learner, method)(path, json=body)
        assert resp.status_code == 401, f"a learner reached {method.upper()} {path}: {resp.status_code}"
    assert len(await _rows()) == 1


async def test_catalog_writes_need_the_csrf_header(client):
    resp = await client._c.post(CATALOGS, json={"name": "No header"})
    assert resp.status_code == 403
    assert error_of(resp)["code"] == "csrf_failed"
    assert len(await _rows()) == 0


async def test_every_catalog_write_leaves_an_audit_row(client):
    catalog = await _catalog(client)
    question = await _question(client)
    added = await _add_items(client, catalog["id"], await _reference("question", question["id"]))
    item_id = added["catalog"]["items"][0]["id"]
    await client.patch(f"{CATALOGS}/{catalog['id']}", json={"name": "Renamed"})
    await client.post(f"{CATALOGS}/{catalog['id']}/status", json={"status": "ready"})
    await client.post(f"{CATALOGS}/{catalog['id']}/items/reorder", json={"item_ids": [item_id]})
    await client.delete(f"{CATALOGS}/items/{item_id}")
    await client.delete(f"{CATALOGS}/{catalog['id']}")

    async with SessionLocal() as db:
        actions = [
            row[0]
            for row in (
                await db.execute(select(AuditLog.action).where(AuditLog.action.like("catalog.%")))
            ).all()
        ]
    assert actions == [
        "catalog.create",
        "catalog.items.add",
        "catalog.update",
        "catalog.status",
        "catalog.reorder",
        "catalog.item.remove",
        "catalog.trash",
    ]


# --------------------------------------------------------------------------- #
# Reading helpers (a catalog may name a block of a passage)
# --------------------------------------------------------------------------- #


async def _reading(client, title: str = "A day at the market") -> dict:
    resp = await client.post(READING, json={"title": title, "body": "The quick brown fox jumps.", "language": "en"})
    assert resp.status_code == 201, resp.text
    return resp.json()


async def _reading_question(client, reading_id: str, prompt: str) -> dict:
    return await _question(client, prompt, context_kind="reading_bound", reading_id=reading_id)


async def _set(client, reading_id: str, title: str) -> dict:
    resp = await client.post(f"{READING}/{reading_id}/sets", json={"title": title})
    assert resp.status_code == 201, resp.text
    return resp.json()


async def _file(client, set_id: str, *question_ids: str) -> dict:
    resp = await client.post(f"{READING}/sets/{set_id}/questions", json={"question_ids": list(question_ids)})
    assert resp.status_code == 200, resp.text
    return resp.json()


async def test_a_catalog_with_no_items_reads_as_empty_rather_than_as_a_failure(client):
    catalog = await _catalog(client)
    read = await _detail(client, catalog["id"])
    assert read["items"] == []
    assert read["item_count"] == 0
    assert read["unavailable_count"] == 0
    assert read["counts"] == {}
