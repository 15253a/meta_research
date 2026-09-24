from dataclasses import replace
import os
from pathlib import Path
import tracemalloc
import pytest
from meta_research.owners.common import OwnerConflict
from meta_research.research_content import discover_literature, read_content
from test_reasoning_summary_decision_flow import runtime_at, seed_checkpoint, SummarySkill
from test_public_first_question_deepfetch import DeterministicDeepFetchProvider
from conftest import _isolate_platform_power_dependency


class CorpusProvider(DeterministicDeepFetchProvider):
    paper_count = 8
    repeats = 120000

    def execute(self,request):
        result=self.result()
        papers=[];fulltexts=[]
        for i in range(self.paper_count):
            url=f"https://example.org/large/{i}"
            papers.append({"title":f"Large paper {i}","url":url,"doi":f"10.1000/large.{i}","source_kind":"publisher","fulltext_status":"accepted","retrieved_at":"2026-09-23T00:00:00Z"})
            fulltexts.append({"paper_url":url,"media_type":"text/plain","content":f"Paper {i}\n"+"文献结果与限制。"*self.repeats})
        return replace(result,papers=tuple(papers),fulltexts=tuple(fulltexts),papers_ledger=None)


def make_corpus(path, provider=None):
    runtime=runtime_at(path,SummarySkill("decline"),provider or CorpusProvider())
    request,checkpoint=seed_checkpoint(runtime)
    for _ in range(10):
        runtime.autonomous_creation.process_once()
        view=runtime.autonomous_creation.query(checkpoint.checkpoint_ref)
        if view and view["deepfetch"]["status"]=="queued":break
    assert runtime.deepfetch.process_once()
    view=runtime.autonomous_creation.query(checkpoint.checkpoint_ref)
    memory=runtime.owners.research_memory
    snapshot=memory.query_literature_snapshot(view["deepfetch"]["literature_snapshot_ref"])
    revision=memory.ensure_question_literature_revision(question_binding=request.accepted_question,source_snapshot_binding=snapshot.as_context_binding(),idempotency_key="large-literature-revision")
    return runtime,request,snapshot,revision


def test_discovery_and_pages_do_not_load_the_corpus(tmp_path,monkeypatch):
    runtime,request,snapshot,revision=make_corpus(tmp_path/"large")
    try:
        memory=runtime.owners.research_memory
        metadata=memory.read_literature_snapshot_metadata(snapshot.snapshot_ref)
        assert len(metadata["fulltexts"])==8
        assert all("content" not in f for f in metadata["fulltexts"])
        paths=[memory._object_store/f["body"]["path"] for f in metadata["fulltexts"]]
        total=sum(p.stat().st_size for p in paths)
        storage = memory._object_store / "literature-snapshot"
        original_entries = {str(p.relative_to(storage)) for p in storage.rglob("*")}
        assert sum(p.stat().st_size for p in storage.rglob("*.json")) < 128 * 1024
        assert len(list(storage.rglob("*.utf8"))) == 9
        assert total>16*1024*1024
        monkeypatch.setattr(memory,"read_literature_snapshot",lambda *a,**k:pytest.fail("full snapshot API invoked for a bounded read"))
        reads={}
        reader=memory._literature_content_pages
        original=reader._pages.read_page
        class CountReads:
            def __init__(self,stream,path):self.stream,self.path=stream,path
            def __getattr__(self,name):return getattr(self.stream,name)
            def read(self,n=-1):
                assert 0<=n<=1024*1024
                data=self.stream.read(n);reads[self.path]=reads.get(self.path,0)+len(data);return data
        def tracked(stream,path,entry,offset,limit):return original(CountReads(stream,str(path)),path,entry,offset,limit)
        monkeypatch.setattr(reader._pages,"read_page",tracked)
        tracemalloc.start()
        page=discover_literature(runtime.owners.research_graph,memory,quest_ref=request.accepted_question.quest_ref,limit=2)
        _,discovery_peak=tracemalloc.get_traced_memory();tracemalloc.reset_peak()
        assert len(page["items"])==2 and page["next_offset"]==2
        assert not reads
        item=page["items"][0]
        content=read_content(runtime.owners.research_graph,memory,quest_ref=request.accepted_question.quest_ref,**item["reader"],offset=0,limit=8192)
        assert content["offset_unit"]=="bytes" and content["returned_bytes"]<=8192
        selected=content["access_path"]
        assert set(reads)=={selected} and reads[selected]==Path(selected).stat().st_size
        first_reads=reads[selected]
        for _ in range(8):
            content=read_content(runtime.owners.research_graph,memory,quest_ref=request.accepted_question.quest_ref,**item["reader"],offset=content["next_offset"],limit=8192)
        _,read_peak=tracemalloc.get_traced_memory();tracemalloc.stop()
        assert reads[selected]-first_reads<=8*8192
        assert discovery_peak<4*1024*1024 and read_peak<8*1024*1024
        assert set(reads)=={selected}
        assert {str(p.relative_to(storage)) for p in storage.rglob("*")} == original_entries
        print({"corpus_bytes":total,"discovery_peak_bytes":discovery_peak,"page_peak_bytes":read_peak,"selected_first_read_bytes":first_reads,"eight_later_pages_bytes":reads[selected]-first_reads})
        # A changed same-size source cannot hide behind a warm signature cache.
        source=Path(selected);before=source.stat()
        with source.open("r+b") as f:f.write(b"X")
        os.utime(source,ns=(before.st_atime_ns,before.st_mtime_ns))
        with pytest.raises(OwnerConflict,match="content_drifted"):
            read_content(runtime.owners.research_graph,memory,quest_ref=request.accepted_question.quest_ref,**item["reader"],limit=8192)
        source.unlink()
        with pytest.raises(OwnerConflict,match="literature_content_unavailable"):
            read_content(runtime.owners.research_graph,memory,quest_ref=request.accepted_question.quest_ref,**item["reader"],limit=8192)
    finally:
        if tracemalloc.is_tracing():tracemalloc.stop()
        runtime.close()

class SmallCorpusProvider(CorpusProvider):
    paper_count = 2
    repeats = 20


def test_exact_literature_pages_recover_and_revalidate_authority(tmp_path, monkeypatch):
    from sqlalchemy import text

    data_root = tmp_path / "restart"
    provider = SmallCorpusProvider()
    runtime, request, snapshot, revision = make_corpus(data_root, provider)
    snapshot_ref = snapshot.snapshot_ref
    quest_ref = request.accepted_question.quest_ref
    full = runtime.owners.research_memory.read_literature_snapshot(snapshot_ref)
    assert len(full["fulltexts"]) == 2
    assert full["fulltexts"][0]["content"] == provider.execute(None).fulltexts[0]["content"]
    runtime.close()
    runtime = runtime_at(data_root, SummarySkill("decline"), provider)
    try:
        memory = runtime.owners.research_memory
        monkeypatch.setattr(memory, "read_literature_snapshot", lambda *a, **k: pytest.fail("bounded read used full corpus"))
        metadata = memory.read_literature_snapshot_metadata(snapshot_ref)
        item = discover_literature(runtime.owners.research_graph, memory, quest_ref=quest_ref, limit=1)["items"][0]
        args = {"quest_ref": quest_ref, "source_ref": snapshot_ref, "version_ref": snapshot_ref}
        summary = read_content(runtime.owners.research_graph, memory, **args)
        assert summary["text"] == full["summary"]
        descriptor = metadata["fulltexts"][0]
        entry_path = "fulltexts/" + descriptor["content_hash"]
        body = read_content(runtime.owners.research_graph, memory, **args, entry_path=entry_path)
        assert body["text"] == full["fulltexts"][0]["content"]
        assert body["entry_hash"] == descriptor["body"]["sha256"]
        with pytest.raises(OwnerConflict, match="content_entry_invalid"):
            read_content(runtime.owners.research_graph, memory, quest_ref=quest_ref, **item["reader"], entry_path="fulltexts/wrong")
        with pytest.raises(OwnerConflict, match="content_source_unbound"):
            memory.read_literature_content_page(snapshot_ref, record_ref=item["reader"]["source_ref"], evidence_basis_ref="forged")

        with memory._database.read() as connection:
            row = connection.execute(text("SELECT receipt_hash, fulltexts_object_path FROM rm_literature_snapshots WHERE snapshot_ref=:ref"), {"ref": snapshot_ref}).first()
        with memory._database.write() as connection:
            connection.execute(text("UPDATE rm_literature_snapshots SET receipt_hash=:value WHERE snapshot_ref=:ref"), {"value": "0" * 64, "ref": snapshot_ref})
        with pytest.raises(OwnerConflict, match="literature_snapshot_invalid"):
            memory.read_literature_snapshot_metadata(snapshot_ref)
        with pytest.raises(OwnerConflict, match="literature_snapshot_invalid"):
            memory.read_literature_content_page(snapshot_ref)
        with memory._database.write() as connection:
            connection.execute(text("UPDATE rm_literature_snapshots SET receipt_hash=:value WHERE snapshot_ref=:ref"), {"value": row.receipt_hash, "ref": snapshot_ref})
        index_path = memory._object_store / row.fulltexts_object_path
        index = index_path.read_bytes()
        index_path.write_text("{}")
        with pytest.raises(OwnerConflict, match="literature_snapshot_custody_unavailable"):
            memory.read_literature_snapshot_metadata(snapshot_ref)
        index_path.write_bytes(index)
        source = memory._object_store / descriptor["body"]["path"]
        replacement = tmp_path / "replaced-body.txt"
        source.replace(replacement)
        source.symlink_to(replacement)
        with pytest.raises(OwnerConflict, match="literature_content_unavailable"):
            memory.read_literature_content_page(snapshot_ref, entry_path=entry_path)
    finally:
        runtime.close()


def test_full_snapshot_rejects_mutation_between_cached_validation_and_full_read(tmp_path, monkeypatch):
    runtime, request, snapshot, revision = make_corpus(tmp_path / "full-read-race", SmallCorpusProvider())
    try:
        memory = runtime.owners.research_memory
        reader = memory._literature_content_pages
        original = reader._pages.read_page
        changed = []
        def mutate_after_validation(source, path, body, offset, limit):
            result = original(source, path, body, offset, limit)
            before = path.stat()
            with path.open("r+b") as writer:
                writer.write(b"X" * before.st_size)
            os.utime(path, ns=(before.st_atime_ns, before.st_mtime_ns))
            changed.append(path)
            return result
        monkeypatch.setattr(reader._pages, "read_page", mutate_after_validation)
        with pytest.raises(OwnerConflict, match="content_drifted"):
            memory.read_literature_snapshot(snapshot.snapshot_ref)
        assert changed
    finally:
        runtime.close()
