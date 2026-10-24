"""Bounded declarative evidence engine; crypto cannot establish HTTP confirmation."""
import hashlib
import json
import math
import regex
import yaml

TIERS=['unknown','candidate','crypto_candidate','probable','http_confirmed']
OPS={'equals','contains','regex','in','exists','sha256'}

def field_value(value,path):
    def walk(value,parts):
        if not parts:return value
        if isinstance(value,list):
            result=[]
            for item in value:
                found=walk(item,parts)
                if found is not None:result.extend(found if isinstance(found,list) else [found])
            return result or None
        return walk(value.get(parts[0]),parts[1:]) if isinstance(value,dict) else None
    return walk(value,path.split('.'))

def validate_rule(rule):
    if not isinstance(rule,dict) or rule.get('version')!=1:raise ValueError('Template version must be 1')
    if not regex.fullmatch(r'[a-z0-9][a-z0-9_-]{0,63}',str(rule.get('id',''))):raise ValueError('Invalid template ID')
    classification=rule.get('classification',{})
    for tier in [classification.get('on_match'),classification.get('maximum_tier',classification.get('on_match'))]:
        if tier not in TIERS[1:]+['contradicted']:raise ValueError('Invalid classification tier')
    if not isinstance(classification.get('product'),str) or not classification['product']:raise ValueError('Product required')
    threshold=rule.get('threshold',0)
    if not isinstance(threshold,(int,float)) or not math.isfinite(threshold) or threshold<0:raise ValueError('Invalid threshold')
    count=0
    def visit(node,depth=0):
        nonlocal count
        count+=1
        if not isinstance(node,dict) or depth>16 or count>256:raise ValueError('Rule too complex')
        for op in ['all','any','not']:
            if op in node:
                if len(node)!=1:raise ValueError('Boolean node must have one operator')
                children=[node[op]] if op=='not' else node[op]
                if not isinstance(children,list) or not children:raise ValueError('Boolean children required')
                for child in children:visit(child,depth+1)
                return
        if node.get('op') not in OPS or not isinstance(node.get('field'),str):raise ValueError('Invalid predicate')
        if len(node['field'])>256 or len(node['field'].split('.'))>32:raise ValueError('Field path too long')
        if node['op']!='exists' and 'value' not in node and 'dataset' not in node:raise ValueError('Predicate value required')
        weight=node.get('weight',1)
        if not isinstance(weight,(int,float)) or not math.isfinite(weight) or not 0<=weight<=1000:raise ValueError('Invalid weight')
        if node['op']=='regex':
            if len(str(node.get('value','')))>1024:raise ValueError('Regex too long')
            regex.compile(node['value'])
    visit(rule.get('match'))
    if 'negative' in rule:visit(rule['negative'])
    if not isinstance(rule.get('datasets',{}),dict) or len(json.dumps(rule,allow_nan=False))>262144:raise ValueError('Invalid datasets or excessive size')
    return rule

def load_rule(text):
    if len(text)>262144:raise ValueError('Template too large')
    return validate_rule(yaml.safe_load(text))

def evaluate(rule,observation):
    evidence=[];errors=[]
    def visit(node):
        if 'all' in node:return all([visit(child) for child in node['all']])
        if 'any' in node:return any([visit(child) for child in node['any']])
        if 'not' in node:return not visit(node['not'])
        actual=field_value(observation,node['field']);expected=node.get('value');op=node['op']
        if 'dataset' in node:expected=rule.get('datasets',{}).get(node['dataset'],[])
        matched=False
        for value in actual if isinstance(actual,list) else [actual]:
            if op=='exists':found=(value is not None)==bool(expected if expected is not None else True)
            elif value is None:found=False
            elif op=='equals':found=value==expected
            elif op=='contains':found=isinstance(value,str) and str(expected) in value
            elif op=='in':found=isinstance(expected,list) and value in expected
            elif op=='sha256':found=isinstance(value,str) and hashlib.sha256(value.encode()).hexdigest()==expected
            else:
                try:found=bool(regex.search(str(expected),str(value)[:1048576],timeout=.02))
                except TimeoutError:found=False;errors.append('regex_timeout')
            matched=matched or found
        evidence.append({'field':node['field'],'op':op,'matched':matched,'weight':node.get('weight',1)})
        return matched
    matched=visit(rule['match']);positive=list(evidence)
    negative=visit(rule['negative']) if rule.get('negative') else False
    score=sum(e['weight'] for e in positive if e['matched'])
    matched=matched and score>=rule.get('threshold',0) and not errors
    tier=rule['classification']['on_match'] if matched else 'unknown'
    if matched and negative:tier='contradicted'
    http=any(e['matched'] and e['field'].startswith('http.') for e in positive) and observation.get('http',{}).get('status_code') is not None
    if tier in {'probable','http_confirmed'} and not http:tier='crypto_candidate'
    maximum=rule['classification'].get('maximum_tier',tier)
    if tier in TIERS and maximum in TIERS:tier=TIERS[min(TIERS.index(tier),TIERS.index(maximum))]
    return {'template_id':rule['id'],'product':rule['classification']['product'],'tier':tier,'score':score,
            'negative':negative,'evidence':evidence,'errors':errors,'template_sha256':hashlib.sha256(json.dumps(rule,sort_keys=True).encode()).hexdigest()}
