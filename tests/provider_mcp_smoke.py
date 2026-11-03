"""Opt-in real Codex MCP isolation smoke; no account, model or thread starts.

Use --codex /absolute/path/to/trusted/native/codex. Generates only synthetic
configuration and a marker MCP executable in a temporary home. Neither inventory
nor protected discovery may start it; the user config must stay unchanged.
"""
import argparse
import json
import os
from pathlib import Path
import sys
import tempfile

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
from auto_providers import ProviderRegistry


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--codex',required=True)
    args=parser.parse_args()
    executable=Path(args.codex).resolve(strict=True)
    if executable.name not in ('codex','codex.exe'):
        raise SystemExit('Supply a trusted native Codex executable')
    original=dict(os.environ)
    with tempfile.TemporaryDirectory(prefix='collab-mcp-smoke-') as directory:
        root=Path(directory);home=root/'home';home.mkdir();workspace=root/'workspace';workspace.mkdir()
        marker=root/'mcp-started';script=root/'mcp-marker.py'
        script.write_text('from pathlib import Path\nPath('+repr(str(marker))+').write_text("started")\n')
        names=('synthetic','a.b','quoted"name')
        content='model_provider="synthetic"\nmodel="synthetic-model"\n[model_providers.synthetic]\nname="Synthetic"\nbase_url="http://127.0.0.1:9/v1"\nwire_api="responses"\nenv_key="LOCAL_API_KEY"\n'
        for name in names:
            content+='[mcp_servers.'+json.dumps(name)+']\ncommand='+json.dumps(sys.executable)+'\nargs=['+json.dumps(str(script))+']\n'
        config=home/'config.toml';config.write_text(content)
        before=config.read_bytes()
        env={k:v for k,v in original.items() if k in ('SYSTEMROOT','WINDIR','TEMP','TMP')}
        env.update(HOME=str(home),USERPROFILE=str(home),CODEX_HOME=str(home),
            PATH=str(executable.parent)+os.pathsep+str(Path(sys.executable).parent)+os.pathsep+os.defpath,
            LOCAL_API_KEY='synthetic-not-a-credential',
            HTTP_PROXY='http://127.0.0.1:9',HTTPS_PROXY='http://127.0.0.1:9',ALL_PROXY='http://127.0.0.1:9',NO_PROXY='')
        try:
            os.environ.clear();os.environ.update(env)
            registry=ProviderRegistry(timeout=15)
            record=next(p for p in registry.discover(refresh=True) if p['id']=='codex')
            assert record['planning_isolation']=='provider_read_only_profile',record.get('unavailable_reason')
            assert record['auth_status']=='configured',record['auth_status']
            assert record['mcp_disabled_count']==len(names)
            for phase in ('planning','worker'):
                launch=registry.build_launch('codex',prompt='No execution: synthetic launch contract only',cwd=workspace,phase=phase)
                assert set(launch.transport['disabled_mcp'])==set(names)
            assert not marker.exists(),'Configured MCP was started'
            assert config.read_bytes()==before,'User config was changed'
            print(json.dumps({'real_cli_mcp_isolation':'passed','server_count':len(names),
                'marker_started':False,'user_config_unchanged':True,'model_or_thread_started':False}))
        finally:
            os.environ.clear();os.environ.update(original)


if __name__=='__main__':main()
