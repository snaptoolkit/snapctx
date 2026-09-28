from pathlib import Path
import numpy as np
from snapctx.api import index_root
from snapctx.index import Index, db_path_for


def setup_index(tmp_path):
    (tmp_path/'m.py').write_text('def first(): return 1\ndef second(): return 2\n')
    index_root(tmp_path)
    idx=Index(db_path_for(tmp_path))
    idx.conn.execute('DELETE FROM symbol_vectors')
    idx.upsert_vectors(['m:first','m:second'], [[1,0],[0,1]])
    return idx


def test_reuses_matrix_across_index_handles(tmp_path):
    idx=setup_index(tmp_path)
    expected=idx.vector_search(np.array([1,0]), 2)
    idx.close()
    fresh=Index(db_path_for(tmp_path)); queries=[]
    fresh.conn.set_trace_callback(queries.append)
    actual=fresh.vector_search(np.array([1,0]),2)
    fresh.close()
    assert [(r['qname'],s) for r,s in actual] == [(r['qname'],s) for r,s in expected]
    assert not any('SELECT qname, vector FROM symbol_vectors' in q for q in queries)


def test_invalidates_after_same_and_other_connection_writes(tmp_path):
    idx=setup_index(tmp_path)
    def top(): return idx.vector_search(np.array([1,0]),2)[0][0]['qname']
    assert top()=='m:first'
    idx.upsert_vectors(['m:first','m:second'], [[0,1],[1,0]])
    assert top()=='m:second'
    other=Index(db_path_for(tmp_path))
    other.upsert_vectors(['m:first','m:second'], [[1,0],[0,1]])
    assert top()=='m:first'
    other.conn.execute("DELETE FROM symbol_vectors WHERE qname='m:first'")
    assert top()=='m:second'
    other.close();idx.close()


def test_uncommitted_changes_never_escape_or_reuse_shared_snapshot(tmp_path):
    idx=setup_index(tmp_path)
    idx.vector_search(np.array([1,0]),2)
    other=Index(db_path_for(tmp_path))
    idx.conn.execute('BEGIN')
    idx.conn.execute("DELETE FROM symbol_vectors WHERE qname='m:first'")
    assert idx.vector_search(np.array([1,0]),2)[0][0]['qname']=='m:second'
    assert other.vector_search(np.array([1,0]),2)[0][0]['qname']=='m:first'
    idx.conn.execute('ROLLBACK')
    assert idx.vector_search(np.array([1,0]),2)[0][0]['qname']=='m:first'
    other.close();idx.close()


def test_cache_is_bounded_and_reloads_after_eviction(tmp_path, monkeypatch):
    from snapctx import _vector_cache
    _vector_cache.clear_vector_cache()
    monkeypatch.setattr(_vector_cache, '_MAX_BYTES', 1)
    idx=setup_index(tmp_path)
    assert idx.vector_search(np.array([1,0]),2)[0][0]['qname']=='m:first'
    queries=[];idx.conn.set_trace_callback(queries.append)
    idx.vector_search(np.array([1,0]),2)
    assert any('SELECT qname, vector FROM symbol_vectors' in q for q in queries)
    idx.close()


def test_empty_snapshot_is_invalidated_when_vectors_arrive(tmp_path):
    idx=setup_index(tmp_path)
    idx.conn.execute('DELETE FROM symbol_vectors')
    assert idx.vector_search(np.array([1,0]),2)==[]
    idx.upsert_vectors(['m:first'],[[1,0]])
    assert idx.vector_search(np.array([1,0]),2)[0][0]['qname']=='m:first'
    assert idx.vector_search(np.array([1,0]),2,kind='class')==[]
    idx.close()


def test_concurrent_readers_keep_identical_rankings(tmp_path):
    from concurrent.futures import ThreadPoolExecutor
    idx=setup_index(tmp_path);idx.close()
    def read(_):
        reader=Index(db_path_for(tmp_path))
        try:return [(r['qname'],s) for r,s in reader.vector_search(np.array([1,0]),2)]
        finally:reader.close()
    with ThreadPoolExecutor(max_workers=4) as pool:
        results=list(pool.map(read,range(12)))
    assert all(r==results[0] for r in results)


def test_replaced_database_does_not_reuse_previous_matrix(tmp_path):
    import os
    from snapctx import _vector_cache
    idx=setup_index(tmp_path)
    idx.vector_search(np.array([1,0]),2)
    idx.conn.execute('PRAGMA wal_checkpoint(TRUNCATE)')
    idx.close()
    replacement=tmp_path/'replacement'
    replacement.mkdir()
    fresh=setup_index(replacement)
    fresh.upsert_vectors(['m:first','m:second'],[[0,1],[1,0]])
    fresh.conn.execute('PRAGMA wal_checkpoint(TRUNCATE)');fresh.close()
    os.replace(db_path_for(replacement),db_path_for(tmp_path))
    reopened=Index(db_path_for(tmp_path))
    assert reopened.vector_search(np.array([1,0]),2)[0][0]['qname']=='m:second'
    reopened.close();_vector_cache.clear_vector_cache()
