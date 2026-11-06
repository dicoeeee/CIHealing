"""Native tool and executable-oracle calibration; zero model requests."""
import argparse
from pathlib import Path
import subprocess

from common import read_json, read_regular, run_logged, snapshot, write_json, write_new
from native_repair import load_config, doctor
from host_sandbox import environment, policy
import cleanup_cases


def project(config, trial, name, action='verify'):
    repo=trial/'visible/scene/sourcecode'; out=trial/name; out.mkdir()
    for folder in ('tmp','java-home'):(out/folder).mkdir()
    write_new(out/'sandbox.sb',policy(config,read=[repo],write=[repo,out]))
    return run_logged(['/usr/bin/sandbox-exec','-f',str(out/'sandbox.sb'),config['python'],'-B',str(repo/'ci_verify.py'),action],
                      out/'execution',30,env=environment(config,out),cwd=repo)


def run(root):
    root=Path(root).resolve();config=load_config(read_json(root/'runtime.json'));results={}
    for version in ('A','B'):
        result=doctor(config,root/'preflight'/version,skill_source=root/'skills'/version)
        write_json(root/'preflight'/f'{version}.json',result);results[version]=result['boundary_canary']
        print('native preflight',version,result['boundary_canary'],flush=True)
    for scenario in cleanup_cases.SCENARIOS:
        trial=root/'offline-fixtures'/scenario;cleanup_cases.materialize(trial,scenario,config)
        repo=trial/'visible/scene/sourcecode'
        baseline=cleanup_cases.probe(config,repo,trial/'baseline',scenario,baseline=True)
        assert baseline['status']=='passed',baseline
        original=(repo/'.build/preexisting.txt').read_bytes()
        if scenario=='generated-delivery':
            (repo/'rules.json').write_text('{"prefix":"new"}\n')
        else:(repo/'app.py').write_text("def route(payload):\n    return 'new:' + payload\n")
        if scenario=='stale-artifact':
            stale=project(config,trial,'stale-check')
            assert stale['returncode']==1
            assert project(config,trial,'required-clean','clean')['returncode']==0
        if scenario=='rollback-preserves-job':
            (repo/'audit.py').write_bytes(read_regular(repo/'.ci/audit.before.py'))
        assert project(config,trial,'positive-project')['returncode']==0
        positive=cleanup_cases.probe(config,repo,trial/'positive-oracle',scenario)
        assert positive['status']=='passed',positive
        assert (repo/'.build/preexisting.txt').read_bytes()==original
        if scenario=='generated-delivery': (repo/'generated_routes.py').unlink()
        elif scenario=='stale-artifact':
            (repo/'app.py').write_text("def route(payload):\n    return 'old:' + payload\n")
            (repo/'.build/route-cache.json').write_text('{"prefix":"new:"}\n')
            assert project(config,trial,'false-green-project')['returncode']==0
        else:(repo/'audit.py').write_text('JOB_VALUE = 5\n')
        negative=cleanup_cases.probe(config,repo,trial/'negative-oracle',scenario)
        assert negative['status']=='failed',negative
        results[scenario]={'baseline_reproduced':True,'valid_repair_passes':True,'counterexample_rejected':True}
        print('oracle preflight',scenario,'passed',flush=True)
    # Exercise the exact isolated registration adapter for all declared scenarios.
    for version in ('A','B'):
        rows=read_json(root/version/'manifest.json')['trials']
        seen=set()
        for row in rows:
            if row['group']!='enterprise' or row['scenario'] in seen:continue
            seen.add(row['scenario']);trial=Path(row['trial'])
            output=root/'preflight'/('enterprise-'+version+'-'+row['scenario'])
            process=run_logged([config['python'],'-B',str(Path(__file__).parent/'ab_skill_host.py'),str(trial/'prepared-layout.json')],output,30)
            assert process['returncode']==0,output
            receipt=read_json(output/'stdout.log')
            assert receipt['status']=='passed' and receipt['model_calls']==0,receipt
    write_json(root/'offline-preflight.json',{'status':'passed','model_calls':0,'results':results,'enterprise_canaries':12})


if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('--root',type=Path,required=True);a=p.parse_args();run(a.root)
