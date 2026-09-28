"""Bounded, evidence-linked context dependencies; no repository-specific rules."""
from pathlib import Path
import pytest
from snapctx.api import context, index_root


def build(tmp_path):
    (tmp_path / 'app').mkdir()
    (tmp_path / 'app/__init__.py').write_text('')
    (tmp_path / 'app/policy.py').write_text('DOWNLOAD_LIMIT = 4\n')
    (tmp_path / 'app/unused.py').write_text('UNUSED = 2\n')
    (tmp_path / 'app/views.py').write_text(
        'from .policy import DOWNLOAD_LIMIT\nfrom .unused import UNUSED\n'
        'def download(request):\n    return DOWNLOAD_LIMIT\n')
    (tmp_path / 'app/urls.py').write_text(
        'from django.urls import path\nfrom .views import download\n'
        'urlpatterns = [path("download/", download)]\n')
    index_root(tmp_path)
    return tmp_path


def test_context_follows_referenced_import_and_registration(tmp_path):
    root = build(tmp_path)
    result = context('app.views:download', root=root, related_file_limit=4)
    files = {Path(r['file']).name: r for r in result['related_files']}
    assert {'policy.py', 'urls.py'} <= files.keys()
    assert 'unused.py' not in files
    assert all(r['evidence'] for r in files.values())
    assert result['seeds'][0]['qname'] == 'app.views:download'


def test_related_is_bounded_and_can_be_disabled(tmp_path):
    root = build(tmp_path)
    assert len(context('app.views:download', root=root, related_file_limit=1)['related_files']) == 1
    assert 'related_files' not in context('app.views:download', root=root, related_file_limit=0)
    with pytest.raises(ValueError):
        context('app.views:download', root=root, related_file_limit=-1)


def test_relative_ts_import_and_barrel(tmp_path):
    (tmp_path/'src').mkdir()
    (tmp_path/'src/config.ts').write_text('export const ENABLE_DOWNLOAD = true;\n')
    (tmp_path/'src/page.ts').write_text("import { ENABLE_DOWNLOAD } from './config';\nexport function download() { return ENABLE_DOWNLOAD; }\n")
    index_root(tmp_path)
    result=context('src/page:download',root=tmp_path,related_file_limit=4)
    assert any(r['file'].endswith('/config.ts') for r in result['related_files'])


def test_deleted_or_changed_sources_do_not_produce_stale_edges(tmp_path):
    root=build(tmp_path)
    (root/'app/policy.py').unlink()
    out=context('app.views:download',root=root,related_file_limit=4)
    assert not any(r['file'].endswith('/policy.py') for r in out['related_files'])
    (root/'app/views.py').write_text('def download(request):\n    return 0\n')
    out=context('app.views:download',root=root,related_file_limit=4)
    assert not out.get('related_files')


def test_ts_alias_in_referenced_jsonc_config(tmp_path):
    (tmp_path/'src').mkdir()
    (tmp_path/'tsconfig.json').write_text('{"references":[{"path":"./tsconfig.app.json"}]}')
    (tmp_path/'tsconfig.app.json').write_text('''{
        // aliases live in the app config
        "compilerOptions": {"paths": {"@/*": ["./src/*"],},},
    }''')
    (tmp_path/'src/base.ts').write_text('export const client = 1;\n')
    (tmp_path/'src/page.ts').write_text("import { client } from '@/base';\nexport function fetchData() { return client; }\n")
    index_root(tmp_path)
    out=context('src/page:fetchData',root=tmp_path,related_file_limit=4)
    assert any(r['file'].endswith('/base.ts') for r in out['related_files'])


def test_local_imports_only_follow_selected_function(tmp_path):
    (tmp_path/'local.py').write_text('VALUE = 1\n')
    (tmp_path/'unrelated.py').write_text('VALUE = 2\n')
    (tmp_path/'m.py').write_text(
        'def target():\n    from local import VALUE\n    return VALUE\n'
        'def other():\n    from unrelated import VALUE\n    return VALUE\n')
    index_root(tmp_path)
    out=context('m:target',root=tmp_path,related_file_limit=4)
    assert [Path(r['file']).name for r in out['related_files']]==['local.py']


def test_ambiguous_module_is_not_guessed(tmp_path):
    (tmp_path/'app').mkdir()
    (tmp_path/'app/helper.py').write_text('VALUE=1\n')
    (tmp_path/'helper.py').write_text('VALUE=2\n')
    (tmp_path/'app/m.py').write_text('from helper import VALUE\ndef target(): return VALUE\n')
    index_root(tmp_path)
    assert not context('app.m:target',root=tmp_path,related_file_limit=4)['related_files']


def test_api_validates_and_keeps_results_distinct(tmp_path):
    from snapctx.api import related_files
    root=build(tmp_path)
    out=related_files(['app.views:download','app.views:download','unknown'],root=root,limit=4)
    paths=[r['file'] for r in out['related_files']]
    assert len(paths)==len(set(paths))
    for value in [True, -1, 17, 1.5]:
        with pytest.raises(ValueError):related_files([],root=root,limit=value)
    with pytest.raises(ValueError):related_files('not-a-list',root=root)


def test_symlink_outside_root_is_not_followed(tmp_path):
    root=tmp_path/'repo';root.mkdir()
    external=tmp_path/'external.py';external.write_text('VALUE=1\n')
    (root/'dep.py').write_text('VALUE=1\n')
    (root/'m.py').write_text('from dep import VALUE\ndef target(): return VALUE\n')
    index_root(root)
    (root/'dep.py').unlink();(root/'dep.py').symlink_to(external)
    assert not context('m:target',root=root,related_file_limit=4)['related_files']


def test_cli_option_and_multi_root_contract(tmp_path):
    from snapctx.cli import _build_parser
    from snapctx.api import context_multi
    assert _build_parser().parse_args(['context','download']).related_file_limit==0
    assert _build_parser().parse_args(['context','download','--related-file-limit','2']).related_file_limit==2
    roots=[]
    for name in ['a','b']:
        r=tmp_path/name;r.mkdir();build(r);roots.append(r)
    result=context_multi('app.views:download',roots,anchor=tmp_path,related_file_limit=2)
    assert 0<len(result['related_files'])<=2
    assert all('root' in r for r in result['related_files'])


def test_supplement_does_not_evict_existing_outlines_at_budget_boundary(tmp_path, monkeypatch):
    from snapctx.api import _context
    root=build(tmp_path)
    base=context('app.views:download',root=root)
    monkeypatch.setattr(_context,'_SOFT_TOKEN_BUDGET',base['token_estimate']+1)
    result=context('app.views:download',root=root,related_file_limit=4)
    assert result['file_outlines']==base['file_outlines']


def test_from_dot_import_resolves_module_instead_of_package_initializer(tmp_path):
    (tmp_path/'pkg').mkdir()
    (tmp_path/'pkg/__init__.py').write_text('')
    (tmp_path/'pkg/helper.py').write_text('def run(): return 1\n')
    (tmp_path/'pkg/m.py').write_text('from . import helper\ndef target(): return helper.run()\n')
    index_root(tmp_path)
    result=context('pkg.m:target',root=tmp_path,related_file_limit=4)
    assert any(r['file'].endswith('/helper.py') for r in result['related_files'])


def test_related_multi_root_limits_and_tags(tmp_path):
    from snapctx.api import related_files_multi
    roots=[]
    for name in ['one','two']:
        root=tmp_path/name;root.mkdir();build(root);roots.append(root)
    result=related_files_multi(['app.views:download'],roots,limit=3,anchor=tmp_path)
    assert len(result['related_files'])==3
    assert {item['root'] for item in result['related_files']}=={'one','two'}
