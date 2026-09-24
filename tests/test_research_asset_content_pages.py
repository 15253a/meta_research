"""RM pages verify original custody once per unchanged opened-file signature."""
from contextlib import contextmanager
import os

import pytest
from sqlalchemy import text

from meta_research.owners.common import OwnerConflict
from meta_research.owners.research_memory import AssetIntakeRequest
import meta_research.owners.research_memory as rm
from test_public_research_asset_roles import _runtime


@pytest.fixture
def owner(tmp_path):
    runtime=_runtime(tmp_path/'root')
    yield runtime.owners.research_memory
    runtime.close()


def accepted(owner,path,body,*,custody='linked_local',key='source'):
    path.write_bytes(body)
    result=owner.submit_asset_intake(AssetIntakeRequest(source_kind='local_path',
        custody_mode=custody,display_name=path.name,source_locator=str(path),media_type='text/plain'),
        idempotency_key=key)
    assert result.asset is not None
    return result.asset


def count_io(monkeypatch):
    observed={'bytes':0,'hook':None}
    original=rm._open_asset_regular_file
    class File:
        def __init__(self,source):self.source=source
        def fileno(self):return self.source.fileno()
        def seek(self,*args):return self.source.seek(*args)
        def read(self,*args):
            value=self.source.read(*args)
            observed['bytes']+=len(value)
            if value and observed['hook']:
                hook=observed['hook'];observed['hook']=None;hook()
            return value
    @contextmanager
    def opened(*args,**kwargs):
        with original(*args,**kwargs) as source:yield File(source)
    monkeypatch.setattr(rm,'_open_asset_regular_file',opened)
    return observed


@pytest.mark.parametrize('custody',['linked_local','managed'])
def test_real_owner_three_pages_scan_once_and_never_copy_asset(owner,tmp_path,monkeypatch,custody):
    body=b'0123456789abcdef'*(1024*1024//16)
    path=tmp_path/'corpus.txt';asset=accepted(owner,path,body,custody=custody)
    before={str(p.relative_to(owner._object_store)) for p in owner._object_store.rglob('*')}
    io=count_io(monkeypatch);pages=[]
    for offset in (0,8192,16384):
        page=owner.read_asset_content_page(asset.version_ref,offset=offset,limit=8192)
        assert page['text'].encode()==body[offset:offset+8192]
        assert page['source_ref']==page['version_ref']==asset.version_ref
        assert page['content_hash']==asset.content_hash
        pages.append(page)
    assert len(body)<=io['bytes']<=len(body)+3*8192,io
    assert {str(p.relative_to(owner._object_store)) for p in owner._object_store.rglob('*')}==before
    assert path.read_bytes()==body
    print('ASSET_PAGE_IO',{'custody':custody,'asset_bytes':len(body),'returned_bytes':sum(p['returned_bytes'] for p in pages),'read_bytes':io['bytes']})


def test_same_size_mtime_change_and_restoration_reverify_original(owner,tmp_path,monkeypatch):
    path=tmp_path/'mutable.txt';body=b'a'*(2*1024*1024);asset=accepted(owner,path,body)
    io=count_io(monkeypatch);args={'offset':1024*1024,'limit':32}
    assert owner.read_asset_content_page(asset.version_ref,**args)['text']=='a'*32
    original=path.stat()
    with path.open('r+b') as changed:changed.write(b'b')
    os.utime(path,ns=(original.st_atime_ns,original.st_mtime_ns))
    with pytest.raises(OwnerConflict,match='content_drifted'):
        owner.read_asset_content_page(asset.version_ref,**args)
    after_drift=io['bytes']
    with path.open('r+b') as changed:changed.write(b'a')
    os.utime(path,ns=(original.st_atime_ns,original.st_mtime_ns))
    assert owner.read_asset_content_page(asset.version_ref,**args)['text']=='a'*32
    assert io['bytes']-after_drift>=len(body)
    path.unlink()
    with pytest.raises(OwnerConflict,match='content_unavailable'):
        owner.read_asset_content_page(asset.version_ref,**args)


def test_replacing_inode_never_reuses_an_old_signature(owner,tmp_path,monkeypatch):
    path=tmp_path/'replace.txt';body=b'a'*(1024*1024);asset=accepted(owner,path,body)
    io=count_io(monkeypatch);owner.read_asset_content_page(asset.version_ref,limit=32)
    old=path.stat();replacement=tmp_path/'replacement.txt'
    replacement.write_bytes(b'b'+body[1:]);os.utime(replacement,ns=(old.st_atime_ns,old.st_mtime_ns))
    replacement.replace(path);assert path.stat().st_ino!=old.st_ino
    with pytest.raises(OwnerConflict,match='content_drifted'):
        owner.read_asset_content_page(asset.version_ref,offset=4096,limit=32)
    replacement.write_bytes(body);replacement.replace(path)
    before=io['bytes']
    assert owner.read_asset_content_page(asset.version_ref,limit=32)['text']=='a'*32
    assert io['bytes']-before>=len(body)


@pytest.mark.parametrize('warm',[False,True])
def test_concurrent_change_and_restore_during_open_read_cannot_publish_page(owner,tmp_path,monkeypatch,warm):
    path=tmp_path/'concurrent.txt';body=b'a'*(2*1024*1024);asset=accepted(owner,path,body)
    io=count_io(monkeypatch)
    if warm:owner.read_asset_content_page(asset.version_ref,limit=32)
    before=path.stat()
    def transient_change():
        with path.open('r+b') as writer:
            writer.seek(len(body)-1);writer.write(b'b');writer.flush()
            writer.seek(len(body)-1);writer.write(b'a');writer.flush()
        os.utime(path,ns=(before.st_atime_ns,before.st_mtime_ns))
    io['hook']=transient_change
    with pytest.raises(OwnerConflict,match='content_drifted'):
        owner.read_asset_content_page(asset.version_ref,limit=32)
    assert owner.read_asset_content_page(asset.version_ref,limit=32)['text']=='a'*32


def test_warm_byte_signature_never_bypasses_receipt_verification(owner,tmp_path,monkeypatch):
    asset=accepted(owner,tmp_path/'receipt.txt',b'accepted bytes')
    io=count_io(monkeypatch);owner.read_asset_content_page(asset.version_ref,limit=2)
    before=io['bytes']
    with owner._database.write() as connection:
        connection.execute(text('UPDATE rm_asset_versions SET receipt_hash=:hash WHERE version_ref=:ref'),
            {'hash':'0'*64,'ref':asset.version_ref})
    with pytest.raises(OwnerConflict,match='asset_receipt_invalid'):
        owner.read_asset_content_page(asset.version_ref,offset=2,limit=2)
    assert io['bytes']==before


def test_directory_metadata_then_exact_entry_is_bounded(owner,tmp_path,monkeypatch):
    root=tmp_path/'directory';root.mkdir();(root/'a.txt').write_bytes(b'first');(root/'b.txt').write_bytes(b'second')
    asset=owner.submit_asset_intake(AssetIntakeRequest(source_kind='local_path',custody_mode='linked_local',
        display_name='directory',source_locator=str(root)),idempotency_key='directory').asset
    io=count_io(monkeypatch)
    listing=owner.read_asset_content_page(asset.version_ref,limit=1)
    assert listing['offset_unit']=='entries' and listing['next_offset']==1 and io['bytes']==0
    page=owner.read_asset_content_page(asset.version_ref,entry_path='b.txt',offset=1,limit=3)
    assert page['text']=='eco' and page['returned_bytes']==3
    with pytest.raises(OwnerConflict,match='content_entry_invalid'):
        owner.read_asset_content_page(asset.version_ref,entry_path='../b.txt')


def test_verified_signature_lru_evicts_metadata_and_rehashes_next_use(owner,tmp_path,monkeypatch):
    from meta_research.owners.research_asset_content import AssetContentPageReader
    # Small configured capacity exercises eviction through the real RM API.
    owner._asset_content_pages=AssetContentPageReader(owner._object_store,owner._receipt_verifier,max_verified_files=2)
    assets=[accepted(owner,tmp_path/f'{index}.txt',bytes([97+index])*4096,key=str(index)) for index in range(3)]
    io=count_io(monkeypatch)
    for index in (0,1,0,2):
        assert owner.read_asset_content_page(assets[index].version_ref,limit=2)['text']==chr(97+index)*2
    assert io['bytes']==3*4096+2
    before=io['bytes']
    assert owner.read_asset_content_page(assets[1].version_ref,limit=2)['text']=='bb'
    assert io['bytes']-before==4096
