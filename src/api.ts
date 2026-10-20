export type ApiStatus = 'loading' | 'ready' | 'unavailable' | 'invalid';
export type Tender = { id:string; title:string; reference:string; deadline?:string; status?:string };
export type DocumentRecord = { id:string; title:string; version?:string; status?:string };
export type Requirement = { id:string; text:string; category?:string; mandatory?:boolean; status?:string };
const base = (import.meta as ImportMeta & {env:Record<string,string|undefined>}).env.VITE_API_BASE_URL?.replace(/\/$/,'');
export async function request<T>(path:string,apiBase=base):Promise<T>{ if(!apiBase) throw new Error('API_NOT_CONFIGURED'); const response=await fetch(`${apiBase}${path}`,{headers:{Accept:'application/json'}}); if(!response.ok) throw new Error(`HTTP_${response.status}`); return response.json() as Promise<T>; }
export function isRecord(value:unknown):value is Record<string,unknown>{return typeof value==='object'&&value!==null&&!Array.isArray(value)}
export function asArray<T>(v:unknown):T[]|null{return Array.isArray(v)?v as T[]:null}
export async function loadTenders(apiBase=base):Promise<Tender[]>{const data=await request<unknown>('/api/tenders',apiBase); const rows=Array.isArray(data)?data:isRecord(data)?data.items:null; if(!Array.isArray(rows)||!rows.every(r=>isRecord(r)&&typeof r.id==='string'&&typeof r.title==='string'&&typeof r.reference==='string')) throw new Error('INVALID_RESPONSE'); return rows as Tender[];}
