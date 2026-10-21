import argparse
import json
import sys
from .pipeline import run


def main():
    parser=argparse.ArgumentParser(description='CPU-only trajectory quality checks; raw input is never modified.')
    parser.add_argument('--data',default='data/gatcv6/raw',help='Root containing sample/ (default: data/gatcv6/raw)')
    parser.add_argument('--output',default='outputs/balanced')
    parser.add_argument('--config',default=None,help='JSON config overrides; resolved config is always exported')
    parser.add_argument('--policy',choices=['balanced','conservative'])
    args=parser.parse_args()
    try:
        config={}
        if args.config:
            with open(args.config) as f:config=json.load(f)
            if not isinstance(config,dict):raise ValueError('Config must be a JSON object')
            from .pipeline import DEFAULTS
            if set(config)-set(DEFAULTS):raise ValueError('Unknown config keys: '+str(sorted(set(config)-set(DEFAULTS))))
        if args.policy:config['policy']=args.policy
        summary=run(args.data,args.output,config)
    except (OSError,ValueError,TypeError) as exc:
        print('QC invocation failed: '+str(exc),file=sys.stderr);return 2
    print(json.dumps({'decisions':summary['decisions'],'review_budget':summary['review_budget'],'batch_problems':len(summary['batch_problems']),'output':args.output},ensure_ascii=False,indent=2))
    # 0 means scan completed, even when records were excluded. 1 means input/check coverage incomplete.
    return 1 if summary['batch_problems'] or summary['check_status'].get('failed',0) else 0

if __name__=='__main__':sys.exit(main())
