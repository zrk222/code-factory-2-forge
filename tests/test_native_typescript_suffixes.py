"""Native TypeScript module suffixes must use the parser, not fallbacks."""
from pathlib import Path
import pytest
from forgeline.source_scope import language_for,iter_source_files,analyze_source

@pytest.mark.parametrize('suffix',['mts','cts'])
def test_native_suffix_inventory_and_language(tmp_path,suffix):
    file=tmp_path/('native.'+suffix);file.write_text('export function recall(id: string): string { return id; }')
    assert language_for(file)=='typescript'
    assert file in iter_source_files(tmp_path)
    report=analyze_source(file,tmp_path)
    assert report['status']=='ok'
    assert report['function_metrics_status']=='exact'

@pytest.mark.parametrize('suffix',['mts','cts'])
def test_native_suffix_parser_rejects_broken_source(tmp_path,suffix):
    file=tmp_path/('native.'+suffix);file.write_text('export function broken(: string {')
    report=analyze_source(file,tmp_path)
    assert report['status']=='syntax_error'
