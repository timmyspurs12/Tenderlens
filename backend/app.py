"""Minimal authenticated TenderLens API. Standard-library WSGI + SQLite; no assessment logic."""
from __future__ import annotations
import hashlib, hmac, json, os, re, sqlite3, tempfile, uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import unquote
from wsgiref.simple_server import make_server

MAX_UPLOAD = 20 * 1024 * 1024
ALLOWED_MIME = {"text/plain", "application/pdf", "application/vnd.openxmlformats-officedocument.wordprocessingml.document"}
UUID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[1-8][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$", re.I)
SCHEMA = """
PRAGMA foreign_keys=ON;
CREATE TABLE IF NOT EXISTS tenders(id TEXT PRIMARY KEY, workspace TEXT NOT NULL, title TEXT NOT NULL, reference TEXT NOT NULL, deadline TEXT, created_at TEXT NOT NULL, UNIQUE(workspace, reference));
CREATE TABLE IF NOT EXISTS documents(id TEXT PRIMARY KEY, tender_id TEXT NOT NULL REFERENCES tenders(id), workspace TEXT NOT NULL, group_id TEXT NOT NULL, version INTEGER NOT NULL, filename TEXT NOT NULL, content_type TEXT NOT NULL, size INTEGER NOT NULL, sha256 TEXT NOT NULL, blob_path TEXT NOT NULL, supersedes_document_id TEXT REFERENCES documents(id), created_at TEXT NOT NULL, UNIQUE(tender_id, group_id, version));
CREATE TABLE IF NOT EXISTS amendments(id TEXT PRIMARY KEY, tender_id TEXT NOT NULL REFERENCES tenders(id), workspace TEXT NOT NULL, document_id TEXT NOT NULL REFERENCES documents(id), predecessor_id TEXT REFERENCES documents(id), relationship TEXT NOT NULL, created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS requirements(id TEXT PRIMARY KEY, tender_id TEXT NOT NULL REFERENCES tenders(id), workspace TEXT NOT NULL, code TEXT NOT NULL, text TEXT NOT NULL, category TEXT NOT NULL, mandatory INTEGER NOT NULL, kind TEXT NOT NULL, created_at TEXT NOT NULL, UNIQUE(tender_id, code));
CREATE TABLE IF NOT EXISTS evidence(id TEXT PRIMARY KEY, tender_id TEXT NOT NULL REFERENCES tenders(id), workspace TEXT NOT NULL, requirement_id TEXT REFERENCES requirements(id), document_id TEXT NOT NULL REFERENCES documents(id), document_sha256 TEXT NOT NULL, start_byte INTEGER NOT NULL, end_byte INTEGER NOT NULL, excerpt TEXT NOT NULL, provenance_json TEXT NOT NULL, created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS evaluation_snapshots(id TEXT PRIMARY KEY, tender_id TEXT NOT NULL REFERENCES tenders(id), workspace TEXT NOT NULL, input_sha256 TEXT NOT NULL, payload_json TEXT NOT NULL, created_at TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS documents_tender ON documents(tender_id, created_at);
CREATE INDEX IF NOT EXISTS evidence_tender ON evidence(tender_id, created_at);
CREATE TRIGGER IF NOT EXISTS documents_no_update BEFORE UPDATE ON documents BEGIN SELECT RAISE(ABORT, 'document versions are immutable'); END;
CREATE TRIGGER IF NOT EXISTS documents_no_delete BEFORE DELETE ON documents BEGIN SELECT RAISE(ABORT, 'document versions are immutable'); END;
CREATE TRIGGER IF NOT EXISTS evidence_no_update BEFORE UPDATE ON evidence BEGIN SELECT RAISE(ABORT, 'evidence provenance is immutable'); END;
CREATE TRIGGER IF NOT EXISTS evidence_no_delete BEFORE DELETE ON evidence BEGIN SELECT RAISE(ABORT, 'evidence provenance is immutable'); END;
CREATE TRIGGER IF NOT EXISTS amendments_no_update BEFORE UPDATE ON amendments BEGIN SELECT RAISE(ABORT, 'amendment annotations are immutable'); END;
CREATE TRIGGER IF NOT EXISTS amendments_no_delete BEFORE DELETE ON amendments BEGIN SELECT RAISE(ABORT, 'amendment annotations are immutable'); END;
CREATE TRIGGER IF NOT EXISTS evaluation_snapshots_no_update BEFORE UPDATE ON evaluation_snapshots BEGIN SELECT RAISE(ABORT, 'evaluation snapshots are immutable'); END;
CREATE TRIGGER IF NOT EXISTS evaluation_snapshots_no_delete BEFORE DELETE ON evaluation_snapshots BEGIN SELECT RAISE(ABORT, 'evaluation snapshots are immutable'); END;
"""
def now() -> str: return datetime.now(timezone.utc).isoformat()
def uid() -> str: return str(uuid.uuid4())
def err(status: int, code: str, message: str): return status, {"error":{"code":code,"message":message}}
def valid_id(v: str) -> bool: return bool(UUID_RE.match(v))
def clean_text(v, field, limit):
    if not isinstance(v,str) or not v.strip() or len(v.strip())>limit: raise ValueError(f"{field} must be a non-empty string of at most {limit} characters")
    return v.strip()
def only_keys(body, allowed):
    unknown=set(body)-set(allowed)
    if unknown: raise ValueError("Unexpected field(s): "+", ".join(sorted(unknown)))

class TenderLensAPI:
    def __init__(self, db_path: str|Path, blob_dir: str|Path, token: str, workspace: str="default"):
        if len(token)<32: raise ValueError("TENDERLENS_API_TOKEN must contain at least 32 characters")
        self.db_path=Path(db_path); self.blob_dir=Path(blob_dir); self.db_path.parent.mkdir(parents=True,exist_ok=True,mode=0o700); self.blob_dir.mkdir(parents=True,exist_ok=True,mode=0o700); os.chmod(self.db_path.parent,0o700); os.chmod(self.blob_dir,0o700)
        self.token=token; self.workspace=workspace
        with self.db() as c: c.executescript(SCHEMA)
        if self.db_path.exists(): os.chmod(self.db_path,0o600)
    @contextmanager
    def db(self):
        c=sqlite3.connect(self.db_path,timeout=10); c.row_factory=sqlite3.Row; c.execute("PRAGMA foreign_keys=ON")
        try:
            with c: yield c
        finally: c.close()
    def __call__(self,environ,start_response):
        method=environ.get("REQUEST_METHOD","GET"); path=unquote(environ.get("PATH_INFO","")); trace=uid()
        if method=="OPTIONS": return self.respond(start_response,204,{})
        auth=environ.get("HTTP_AUTHORIZATION","")
        if not auth.startswith("Bearer ") or not hmac.compare_digest(auth[7:],self.token):
            status,body=err(401,"unauthorized","Valid bearer authentication is required."); return self.respond(start_response,status,body,trace)
        try:
            status,body=self.route(method,path,environ)
        except ValueError as exc: status,body=err(400,"validation_error",str(exc))
        except sqlite3.IntegrityError: status,body=err(409,"conflict","The record conflicts with an existing or referenced record.")
        except Exception: status,body=err(500,"internal_error","Request failed. Contact the service operator with the request ID.")
        return self.respond(start_response,status,body,trace)
    @staticmethod
    def respond(start_response,status,body,trace=""):
        raw=b"" if status==204 else json.dumps(body,separators=(",",":"),ensure_ascii=False).encode()
        phrase={200:"OK",201:"Created",204:"No Content",400:"Bad Request",401:"Unauthorized",404:"Not Found",409:"Conflict",413:"Content Too Large",415:"Unsupported Media Type",500:"Internal Server Error"}.get(status,"Error")
        headers=[("Content-Length",str(len(raw))),("Cache-Control","no-store"),("X-Content-Type-Options","nosniff")]
        if status!=204: headers.append(("Content-Type","application/json; charset=utf-8"))
        if trace: headers.append(("X-Request-ID",trace))
        start_response(f"{status} {phrase}",headers); return [raw]
    def body_json(self,e):
        try:
            n=int(e.get("CONTENT_LENGTH") or 0)
            if n<1 or n>65536: raise ValueError("JSON body must be between 1 byte and 64 KiB")
            body=json.loads(e["wsgi.input"].read(n))
            if not isinstance(body,dict): raise ValueError("JSON body must be an object")
            return body
        except (json.JSONDecodeError,UnicodeDecodeError): raise ValueError("Malformed JSON body")
    def tender(self,c,tid):
        if not valid_id(tid): return None
        return c.execute("SELECT * FROM tenders WHERE id=? AND workspace=?",(tid,self.workspace)).fetchone()
    def route(self,m,p,e):
        if p=="/api/tenders" and m=="GET":
            with self.db() as c: rows=c.execute("SELECT id,title,reference,deadline,created_at FROM tenders WHERE workspace=? ORDER BY created_at DESC",(self.workspace,)).fetchall()
            return 200,{"items":[dict(r) for r in rows]}
        if p=="/api/tenders" and m=="POST":
            b=self.body_json(e); only_keys(b,{"title","reference","deadline"}); title=clean_text(b.get("title"),"title",200); ref=clean_text(b.get("reference"),"reference",120); deadline=b.get("deadline")
            if deadline is not None: deadline=clean_text(deadline,"deadline",80)
            record={"id":uid(),"title":title,"reference":ref,"deadline":deadline,"created_at":now()}
            with self.db() as c: c.execute("INSERT INTO tenders VALUES(?,?,?,?,?,?)",(record["id"],self.workspace,title,ref,deadline,record["created_at"]))
            return 201,record
        snapshot_match=re.fullmatch(r"/api/tenders/([^/]+)/evaluation-snapshots(?:/([^/]+))?",p)
        if snapshot_match:
            tid,snapshot_id=snapshot_match.groups()
            with self.db() as c:
                if self.tender(c,tid) is None: return err(404,"not_found","Tender not found.")
                return self.evaluation_snapshots(c,m,tid,snapshot_id,e)
        match=re.fullmatch(r"/api/tenders/([^/]+)(?:/(documents|amendments|requirements|evidence))?",p)
        if not match: return err(404,"not_found","Route not found.")
        tid,resource=match.groups()
        with self.db() as c:
            tender=self.tender(c,tid)
            if tender is None: return err(404,"not_found","Tender not found.")
            if resource is None and m=="GET": return 200,dict(tender)
            if resource=="documents": return self.documents(c,m,tid,e)
            if resource=="amendments": return self.amendments(c,m,tid,e)
            if resource=="requirements": return self.requirements(c,m,tid,e)
            if resource=="evidence": return self.evidence(c,m,tid,e)
        return err(404,"not_found","Route not found.")
    def evaluation_snapshots(self,c,m,tid,snapshot_id,e):
        if m=="GET":
            if snapshot_id is None:
                rows=c.execute("SELECT id,input_sha256,created_at FROM evaluation_snapshots WHERE tender_id=? AND workspace=? ORDER BY created_at,id",(tid,self.workspace)).fetchall()
                return 200,{"items":[{**dict(r),"status":"prepared_not_submitted"} for r in rows]}
            if not valid_id(snapshot_id): return err(404,"not_found","Evaluation snapshot not found.")
            row=c.execute("SELECT * FROM evaluation_snapshots WHERE id=? AND tender_id=? AND workspace=?",(snapshot_id,tid,self.workspace)).fetchone()
            if row is None: return err(404,"not_found","Evaluation snapshot not found.")
            return 200,{"snapshot_id":row["id"],"input_sha256":row["input_sha256"],"created_at":row["created_at"],"status":"prepared_not_submitted","submission":json.loads(row["payload_json"])}
        if m!="POST" or snapshot_id is not None: return err(404,"not_found","Route not found.")
        b=self.body_json(e); only_keys(b,{"evidence_ids"})
        evidence_ids=b.get("evidence_ids",[])
        if not isinstance(evidence_ids,list) or len(evidence_ids)>100: raise ValueError("evidence_ids must be a list of at most 100 evidence UUIDs")
        if any(not isinstance(value,str) or not valid_id(value) for value in evidence_ids): raise ValueError("Every evidence_id must be a UUID")
        if len(evidence_ids)!=len(set(evidence_ids)): raise ValueError("evidence_ids must be unique")
        req_rows=c.execute("SELECT id,code,text,category,mandatory,kind FROM requirements WHERE tender_id=? AND workspace=? ORDER BY code,id",(tid,self.workspace)).fetchall()
        if not req_rows: raise ValueError("At least one explicit requirement must be registered before preparing an evaluation")
        if len(req_rows)>50: raise ValueError("At most 50 requirements can be included in an evaluation snapshot")
        requirements=[{"id":r["id"],"code":r["code"],"text":r["text"],"category":r["category"],"mandatory":bool(r["mandatory"]),"kind":r["kind"]} for r in req_rows]
        if evidence_ids:
            placeholders=",".join("?" for _ in evidence_ids)
            rows=c.execute(f"SELECT e.id,e.requirement_id,e.document_id,e.document_sha256,e.start_byte,e.end_byte,e.excerpt,e.provenance_json,e.created_at AS evidence_created_at,d.version,d.sha256 AS stored_sha256,d.filename,d.blob_path,d.size FROM evidence e JOIN documents d ON d.id=e.document_id WHERE e.tender_id=? AND e.workspace=? AND e.id IN ({placeholders})",(tid,self.workspace,*evidence_ids)).fetchall()
            if len(rows)!=len(evidence_ids): return err(400,"invalid_evidence","Every evidence record must belong to this tender.")
        else:
            rows=[]
        evidence=[]
        for row in sorted(rows,key=lambda r:(r["evidence_created_at"],r["id"])):
            data=Path(row["blob_path"]).read_bytes()
            if hashlib.sha256(data).hexdigest()!=row["stored_sha256"] or row["document_sha256"]!=row["stored_sha256"]:
                return err(500,"integrity_failure","Stored evidence source did not verify while preparing the snapshot.")
            if row["end_byte"]>row["size"] or data[row["start_byte"]:row["end_byte"]].decode("utf-8")!=row["excerpt"]:
                return err(500,"integrity_failure","Evidence excerpt no longer matches its recorded source range.")
            provenance=json.loads(row["provenance_json"])
            evidence.append({"id":row["id"],"requirement_id":row["requirement_id"],"document_id":row["document_id"],"document_version":row["version"],"document_sha256":row["stored_sha256"],"filename":row["filename"],"start_byte":row["start_byte"],"end_byte":row["end_byte"],"excerpt":row["excerpt"],"provenance":provenance,"created_at":row["evidence_created_at"]})
        if any(len(item["text"])>4000 for item in requirements): raise ValueError("Requirement text exceeds the 4000-character contract limit")
        if any(len(item["excerpt"])>8000 for item in evidence): raise ValueError("Evidence excerpt exceeds the 8000-character contract limit")
        base={"schema_version":"tenderlens.evaluation-submission.v1","evaluation_id":uid(),"snapshot_id":uid(),"tender_id":tid,"requirements":requirements,"evidence":evidence}
        canonical=json.dumps(base,sort_keys=True,separators=(",",":"),ensure_ascii=False)
        digest=hashlib.sha256(canonical.encode("utf-8")).hexdigest()
        submission={**base,"snapshot_sha256":digest}
        payload=json.dumps(submission,sort_keys=True,separators=(",",":"),ensure_ascii=False)
        if len(payload)>250000: raise ValueError("Evaluation snapshot exceeds the 250000-character contract limit")
        created_at=now()
        c.execute("INSERT INTO evaluation_snapshots VALUES(?,?,?,?,?,?)",(submission["snapshot_id"],tid,self.workspace,digest,payload,created_at))
        return 201,{"snapshot_id":submission["snapshot_id"],"input_sha256":digest,"created_at":created_at,"status":"prepared_not_submitted","submission":submission}
    def documents(self,c,m,tid,e):
        if m=="GET":
            rows=c.execute("SELECT id,group_id,version,filename,content_type,size,sha256,supersedes_document_id,created_at FROM documents WHERE tender_id=? AND workspace=? ORDER BY group_id,version",(tid,self.workspace)).fetchall()
            return 200,{"items":[dict(r) for r in rows]}
        if m!="POST": return err(404,"not_found","Route not found.")
        try: size=int(e.get("CONTENT_LENGTH") or 0)
        except ValueError: size=MAX_UPLOAD+1
        if size<1: raise ValueError("Document body is required")
        if size>MAX_UPLOAD: return err(413,"file_too_large","Maximum upload size is 20 MiB.")
        mime=e.get("CONTENT_TYPE","").split(";",1)[0].strip().lower()
        if mime not in ALLOWED_MIME: return err(415,"unsupported_media_type","Allowed formats: text/plain, application/pdf, DOCX.")
        filename=clean_text(e.get("HTTP_X_FILENAME","document"),"X-Filename",180)
        if any(ord(ch)<32 for ch in filename) or "/" in filename or "\\" in filename: raise ValueError("Filename must be a plain file name")
        group=clean_text(e.get("HTTP_X_DOCUMENT_GROUP","main"),"X-Document-Group",120)
        supersedes=e.get("HTTP_X_SUPERSEDES_DOCUMENT_ID") or None
        if supersedes:
            old=c.execute("SELECT id,group_id FROM documents WHERE id=? AND tender_id=? AND workspace=?",(supersedes,tid,self.workspace)).fetchone()
            if old is None: return err(400,"invalid_predecessor","Predecessor must be a document version in this tender.")
            if old["group_id"]!=group: return err(400,"invalid_predecessor","Predecessor must belong to the same document group.")
        data=e["wsgi.input"].read(size)
        if len(data)!=size: raise ValueError("Upload length does not match Content-Length")
        digest=hashlib.sha256(data).hexdigest(); dest=self.blob_dir/digest
        if not dest.exists():
            with tempfile.NamedTemporaryFile(dir=self.blob_dir,delete=False) as f: f.write(data); temp=f.name
            os.replace(temp,dest)
        ver=c.execute("SELECT COALESCE(MAX(version),0)+1 FROM documents WHERE tender_id=? AND group_id=?",(tid,group)).fetchone()[0]
        record={"id":uid(),"group_id":group,"version":ver,"filename":filename,"content_type":mime,"size":size,"sha256":digest,"supersedes_document_id":supersedes,"created_at":now()}
        c.execute("INSERT INTO documents VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",(record["id"],tid,self.workspace,group,ver,filename,mime,size,digest,str(dest),supersedes,record["created_at"]))
        return 201,record
    def amendments(self,c,m,tid,e):
        if m=="GET":
            rows=c.execute("SELECT a.id,a.document_id,a.predecessor_id,a.relationship,a.created_at,d.sha256 FROM amendments a JOIN documents d ON d.id=a.document_id WHERE a.tender_id=? AND a.workspace=? ORDER BY a.created_at",(tid,self.workspace)).fetchall()
            return 200,{"items":[dict(r) for r in rows],"comparison_status":"unavailable_without_verified_comparison_engine"}
        if m!="POST": return err(404,"not_found","Route not found.")
        b=self.body_json(e); only_keys(b,{"document_id","predecessor_id","relationship"}); doc=clean_text(b.get("document_id"),"document_id",36); pred=b.get("predecessor_id"); relation=b.get("relationship")
        if not valid_id(doc) or pred is not None and (not isinstance(pred,str) or not valid_id(pred)): raise ValueError("Document references must be UUIDs")
        if relation not in {"explicit_replacement","conflicting","clarification","unresolved","none"}: raise ValueError("relationship must be explicit_replacement, conflicting, clarification, unresolved, or none")
        child=c.execute("SELECT id FROM documents WHERE id=? AND tender_id=? AND workspace=?",(doc,tid,self.workspace)).fetchone()
        if child is None: return err(400,"invalid_document","Amendment document must belong to this tender.")
        if pred:
            parent=c.execute("SELECT id FROM documents WHERE id=? AND tender_id=? AND workspace=?",(pred,tid,self.workspace)).fetchone()
            if parent is None: return err(400,"invalid_predecessor","Predecessor must belong to this tender.")
        record={"id":uid(),"document_id":doc,"predecessor_id":pred,"relationship":relation,"comparison_status":"unavailable_without_verified_comparison_engine","created_at":now()}
        c.execute("INSERT INTO amendments VALUES(?,?,?,?,?,?,?)",(record["id"],tid,self.workspace,doc,pred,relation,record["created_at"]))
        return 201,record
    def requirements(self,c,m,tid,e):
        if m=="GET":
            rows=c.execute("SELECT id,code,text,category,mandatory,kind,created_at FROM requirements WHERE tender_id=? AND workspace=? ORDER BY code",(tid,self.workspace)).fetchall()
            return 200,{"items":[{**dict(r),"mandatory":bool(r["mandatory"])} for r in rows]}
        if m!="POST": return err(404,"not_found","Route not found.")
        b=self.body_json(e); only_keys(b,{"code","text","category","mandatory","kind"}); code=clean_text(b.get("code"),"code",80); text=clean_text(b.get("text"),"text",4000)
        category=b.get("category"); kind=b.get("kind"); mandatory=b.get("mandatory")
        if category not in {"eligibility","mandatory_documentation","technical","interpretive"}: raise ValueError("Unsupported requirement category")
        if kind not in {"objective","judgment"}: raise ValueError("kind must be objective or judgment")
        if not isinstance(mandatory,bool): raise ValueError("mandatory must be a boolean")
        record={"id":uid(),"code":code,"text":text,"category":category,"mandatory":mandatory,"kind":kind,"created_at":now()}
        c.execute("INSERT INTO requirements VALUES(?,?,?,?,?,?,?,?,?)",(record["id"],tid,self.workspace,code,text,category,int(mandatory),kind,record["created_at"]))
        return 201,record
    def evidence(self,c,m,tid,e):
        if m=="GET":
            rows=c.execute("SELECT id,requirement_id,document_id,document_sha256,start_byte,end_byte,excerpt,provenance_json,created_at FROM evidence WHERE tender_id=? AND workspace=? ORDER BY created_at",(tid,self.workspace)).fetchall()
            return 200,{"items":[{**{k:r[k] for k in r.keys() if k!="provenance_json"},"provenance":json.loads(r["provenance_json"])} for r in rows]}
        if m!="POST": return err(404,"not_found","Route not found.")
        b=self.body_json(e); only_keys(b,{"document_id","requirement_id","start_byte","end_byte"}); doc=clean_text(b.get("document_id"),"document_id",36); req=b.get("requirement_id")
        if not valid_id(doc): raise ValueError("document_id must be a UUID")
        d=c.execute("SELECT * FROM documents WHERE id=? AND tender_id=? AND workspace=?",(doc,tid,self.workspace)).fetchone()
        if d is None: return err(400,"invalid_document","Evidence source must be a document version in this tender.")
        if d["content_type"]!="text/plain": return err(400,"unsupported_evidence_source","Byte-range provenance currently supports text/plain documents only; no extracted passage is fabricated.")
        if req is not None:
            if not isinstance(req,str) or not valid_id(req) or c.execute("SELECT 1 FROM requirements WHERE id=? AND tender_id=? AND workspace=?",(req,tid,self.workspace)).fetchone() is None: return err(400,"invalid_requirement","Requirement must belong to this tender.")
        start,end=b.get("start_byte"),b.get("end_byte")
        if type(start) is not int or type(end) is not int or start<0 or end<=start or end>d["size"]: raise ValueError("Evidence byte offsets must be a non-empty in-range interval")
        data=Path(d["blob_path"]).read_bytes()
        if hashlib.sha256(data).hexdigest()!=d["sha256"]: return err(500,"integrity_failure","Stored source hash did not verify.")
        try: excerpt=data[start:end].decode("utf-8")
        except UnicodeDecodeError: raise ValueError("Evidence byte offsets must align to UTF-8 character boundaries")
        provenance={"document_id":doc,"document_sha256":d["sha256"],"document_version":d["version"],"filename":d["filename"],"start_byte":start,"end_byte":end,"retrieved_at":now(),"integrity_check":"sha256_match","source_authenticity":"not_independently_verified"}
        record={"id":uid(),"requirement_id":req,"document_id":doc,"document_sha256":d["sha256"],"start_byte":start,"end_byte":end,"excerpt":excerpt,"provenance":provenance,"created_at":now()}
        c.execute("INSERT INTO evidence VALUES(?,?,?,?,?,?,?,?,?,?,?)",(record["id"],tid,self.workspace,req,doc,d["sha256"],start,end,excerpt,json.dumps(provenance),record["created_at"]))
        return 201,record

def create_app():
    token=os.environ.get("TENDERLENS_API_TOKEN","")
    db=os.environ.get("TENDERLENS_DB_PATH","./var/tenderlens.sqlite3"); blobs=os.environ.get("TENDERLENS_BLOB_DIR","./var/documents")
    return TenderLensAPI(db,blobs,token,os.environ.get("TENDERLENS_WORKSPACE","default"))
if __name__=="__main__":
    app=create_app(); host=os.environ.get("TENDERLENS_HOST","127.0.0.1"); port=int(os.environ.get("TENDERLENS_PORT","8000"))
    with make_server(host,port,app) as server: print(f"TenderLens API listening on {host}:{port}"); server.serve_forever()
