"""Cached lexical retrieval: BM25, query coverage and source diversity."""
from collections import Counter
from functools import lru_cache
import math
from pathlib import Path
import re
import unicodedata

KNOWLEDGE_DIR = Path(__file__).resolve().parent / 'knowledge_base'
SUPPORTED_EXTENSIONS = {'.txt', '.md'}
STOP = set('a o as os de do da dos das e em um uma para por com que qual quais como sobre me diga explique é sao ser se ao aos na no nas nos mais menos porque porquê quem quando onde isso isto suas seus sua seu funciona funcionam'.split())

def normalizar(texto):
    return ''.join(c for c in unicodedata.normalize('NFD',texto.lower()) if unicodedata.category(c) != 'Mn')

STOP = {normalizar(t) for t in STOP}

def termos(texto):
    return [p for p in re.findall(r'[^\W_]+', normalizar(texto)) if len(p)>1 and p not in STOP]

def dividir_texto(texto, tamanho=800, sobreposicao=120):
    if tamanho <= 0 or sobreposicao < 0 or sobreposicao >= tamanho:
        raise ValueError('Tamanho ou sobreposição inválidos.')
    texto = ' '.join(texto.split())
    result=[]; start=0
    while start < len(texto):
        end=min(start+tamanho,len(texto))
        if end < len(texto):
            boundary=texto.rfind(' ',start+max(sobreposicao+1,tamanho//2),end)
            if boundary>start: end=boundary
        result.append(texto[start:end].strip())
        if end==len(texto): break
        start=end-sobreposicao
        while start<end and start>0 and texto[start-1]!=' ': start+=1
    return result

def assinatura():
    if not KNOWLEDGE_DIR.exists(): return ()
    return tuple((str(p.resolve()),p.stat().st_mtime_ns,p.stat().st_size) for p in sorted(KNOWLEDGE_DIR.rglob('*'))
                 if p.is_file() and p.suffix.lower() in SUPPORTED_EXTENSIONS)

@lru_cache(maxsize=2)
def indice(root, signature):
    docs=[]; counts=[]; df=Counter()
    for path,_,_ in signature:
        arquivo=Path(path)
        for n,chunk in enumerate(dividir_texto(arquivo.read_text(encoding='utf-8')),1):
            source=arquivo.relative_to(root).as_posix()
            docs.append({'fonte':source,'trecho':str(n),'conteudo':chunk})
            counter=Counter(termos(chunk));counts.append(counter);df.update(counter.keys())
    avg=sum(sum(c.values()) for c in counts)/max(1,len(counts))
    return docs,counts,df,avg

def carregar_documentos():
    docs,_,_,_=indice(str(KNOWLEDGE_DIR.resolve()),assinatura())
    return [dict(d) for d in docs]

def depende_do_assunto(pergunta):
    return bool(re.search(r'\b(isso|isto|suas|seus|deles|delas|disso|nesse|nessa)\b', normalizar(pergunta)))

def assunto_anterior(linhas):
    """Keep the last explicit topic across a chain of pronoun-only follow-ups."""
    for linha in reversed(linhas):
        if linha['papel'] == 'user' and not depende_do_assunto(linha['conteudo']):
            return linha['conteudo']
    return ''

def consulta_contextual(pergunta, anterior=''):
    clean=normalizar(pergunta).strip()
    # Only borrow a topic for explicit anaphoric follow-ups, not a new question.
    if anterior and depende_do_assunto(pergunta):
        return anterior + ' ' + pergunta
    if re.search(r'\b(coisas|produtos|precos)\b.*\b(caras|caros|sobem|aumentam)\b',clean):
        return 'inflação preços'
    return pergunta

def pesquisar(pergunta, limite=3, anterior=''):
    if limite<=0: return []
    query=set(termos(consulta_contextual(pergunta,anterior)))
    if not query: return []
    docs,counts,df,avg=indice(str(KNOWLEDGE_DIR.resolve()),assinatura())
    results=[];n=len(docs)
    for doc,counter in zip(docs,counts):
        title=set(termos(Path(doc['fonte']).stem))
        matched=query & (counter.keys()|title)
        # Unknown specific terms (e.g. França/2026) must not be discarded.
        if len(matched)/len(query)<0.65: continue
        length=sum(counter.values()); score=0.0
        for word in matched:
            freq=counter[word]
            idf=math.log(1+(n-df[word]+0.5)/(df[word]+0.5))
            score+=idf*(freq*2.2/(freq+1.2*(0.25+0.75*length/max(avg,1))))
            if word in title: score+=2*idf
        if score>0: results.append({**doc,'pontuacao':round(score,4)})
    results.sort(key=lambda r:(-r['pontuacao'],r['fonte'],int(r['trecho'])))
    selected=[];seen=set()
    for r in results:
        if r['fonte'] in seen: continue
        selected.append(r);seen.add(r['fonte'])
        if len(selected)>=limite: break
    return selected
