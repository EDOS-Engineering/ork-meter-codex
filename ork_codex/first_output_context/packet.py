"""Emit one new target per source, merging overlap; never pad the allowance."""
import re
from ork_codex.tokens import count

TOKEN_LIMIT=256
MARKER='\n[source evidence]\n'


def known(row, visible):
    """Conservative source-bound novelty: same path and exact selected span.

    A path listing or a numeric value alone never establishes source visibility.
    Numbered rg/sed output is recognized, with indentation retained.
    """
    for record in visible:
        if not re.search(r'(?<![\w/])'+re.escape(row['path'])+r'(?![\w./])',record):
            continue
        text=row.get('text')
        if text:
            if text.rstrip('\r\n') in record:
                return True
            numbered=[]
            for line in record.splitlines(keepends=True):
                numbered.append(re.sub(r'^(?:'+re.escape(row['path'])+r':)?\s*\d+[:\t ]', '', line))
            if text.rstrip('\r\n') in ''.join(numbered):
                return True
        elif all(re.search(re.escape(row['path'])+r':'+str(n)+r'(?:[:\s]|$)',record)
                 for n in row['locations']):
            return True
    return False


def render(packet, visible=()):
    sections={s:[] for s in packet['sources']};suppressed={s:0 for s in sections}
    # Independence is preserved at channel selection. A/C contain source text;
    # B contains locations only. Combinations merge repeated physical lines.
    for source,rows in packet['sources'].items():
        for row in rows:
            relevant_locations=row.get('locations', [row.get('matched_line')])
            reference_seen=source in ('reference_files','keyword_windows') and any(
                v['path']==row['path'] and v['start_line'] in relevant_locations and known(v,visible)
                for v in packet.get('visibility',[]))
            if known(row,visible) or reference_seen:suppressed[source]+=1
            else:sections[source].append(row)
    for def_lines,window_lines in ((3,3),(1,3),(1,1)):
        files={};selected={s:[] for s in sections}
        def origin(row):
            return files.setdefault(row['path'],dict(lines={},views=[],locations=[]))
        for source,rows in sections.items():
            if not rows:continue
            row=rows[0];entry=origin(row)
            if source=='reference_files':
                entry['locations']=row['locations'][:1]
                entry['symbol']=row['matched_terms'][0]
                entry['views'].append('references')
                selected[source].append(dict(path=row['path'],sha256=row['sha256'],locations=row['locations'][:1]))
                continue
            start=row['start_line'];lines=row['text'].splitlines(keepends=True)
            if source=='definitions':
                lines=lines[:def_lines];label='definition'
            else:
                label='context'
                if window_lines==1:
                    start=row['matched_line'];lines=[lines[start-row['start_line']]]
            excerpt=dict(row,start_line=start,end_line=start+len(lines)-1,text=''.join(lines))
            # Shrinkage must not reintroduce a span already present in a result.
            if known(excerpt,visible):
                continue
            for n,line in enumerate(lines,start):
                if n in entry['lines'] and entry['lines'][n]!=line:raise ValueError('source overlap disagrees')
                entry['lines'][n]=line
            entry['views'].append(label)
            selected[source].append(dict(path=row['path'],sha256=row['sha256'],start_line=start,end_line=start+len(lines)-1))
        text=''
        for path,entry in files.items():
            if not entry['views']:continue
            text+=path+' ('+', '.join(entry['views'])+')\n'
            # Avoid repeating reference locations that are printed with excerpts.
            locations=[n for n in entry['locations'] if n not in entry['lines']]
            if locations:text+='references: '+','.join(map(str,locations))+' '+entry['symbol']+'\n'
            for n,line in sorted(entry['lines'].items()):
                text+=str(n)+' '+line
                if not line.endswith(('\n','\r')):text+='\n'
        if not text:
            return None,dict(reason='no-novel-evidence',tokens=0,suppressed=suppressed,selected=selected)
        value=MARKER+text;tokens=count(value)
        if tokens<=TOKEN_LIMIT:
            return value,dict(tokens=tokens,token_limit=TOKEN_LIMIT,representation='text',
                tokenizer='pinned-o200k_base-local-estimate',selected=selected,suppressed=suppressed,
                scope='One new top candidate per source; overlapping lines shared; full source hashes in local receipts; no padding.')
    return None,dict(reason='minimum-useful-selection-exceeds-token-allowance',tokens=0,token_limit=TOKEN_LIMIT,suppressed=suppressed)
