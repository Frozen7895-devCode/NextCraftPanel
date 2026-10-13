// Client HTTP : jeton CSRF, gestion des erreurs, téléversement avec progression.
let csrf = "";
let handlers = { unauthorized() {}, passwordChange() {} };
export const setCsrf = (t) => { csrf = t || ""; };
export const setAuthHandlers = (h) => { handlers = { ...handlers, ...h }; };

export class ApiError extends Error {
  constructor(status, data) {
    super((data && data.detail) || (typeof data === "string" && data) || `Erreur ${status}`);
    this.status = status; this.data = data || {};
    this.confirmRequired = !!(data && data.confirm_required);
  }
}

export async function api(method, url, body, opts = {}) {
  const headers = {};
  if (method !== "GET") headers["X-CSRF-Token"] = csrf;
  let payload;
  if (body instanceof FormData) payload = body;
  else if (body !== undefined) { headers["Content-Type"] = "application/json"; payload = JSON.stringify(body); }
  let res;
  try { res = await fetch(url, { method, headers, body: payload, credentials: "same-origin", signal: opts.signal }); }
  catch (e) { if (e.name === "AbortError") throw e; throw new ApiError(0, { detail: "Impossible de joindre le panel (connexion perdue ?)." }); }
  const ct = res.headers.get("content-type") || "";
  const data = ct.includes("json") ? await res.json().catch(() => null) : await res.text();
  if (!res.ok) {
    if (res.status === 401 && !opts.noAuthRedirect) handlers.unauthorized();
    if (res.status === 403 && data && data.code === "password_change_required") handlers.passwordChange();
    throw new ApiError(res.status, data);
  }
  return data;
}
export const get = (url, o) => api("GET", url, undefined, o);
export const post = (url, body, o) => api("POST", url, body === undefined ? {} : body, o);
export const put = (url, body, o) => api("PUT", url, body, o);
export const patch = (url, body, o) => api("PATCH", url, body, o);
export const del = (url, o) => api("DELETE", url, undefined, o);
export const qs = (o) => { const p = new URLSearchParams(); for (const [k, v] of Object.entries(o)) if (v !== undefined && v !== null && v !== "") p.set(k, v); const s = p.toString(); return s ? `?${s}` : ""; };

export function upload(url, formData, onProgress) {
  return new Promise((resolve, reject) => {
    const xhr = new XMLHttpRequest();
    xhr.open("POST", url);
    xhr.setRequestHeader("X-CSRF-Token", csrf);
    xhr.upload.onprogress = (e) => { if (e.lengthComputable && onProgress) onProgress(e.loaded / e.total); };
    xhr.onload = () => {
      let data = null; try { data = JSON.parse(xhr.responseText); } catch { /* ignore */ }
      if (xhr.status >= 200 && xhr.status < 300) resolve(data);
      else { if (xhr.status === 401) handlers.unauthorized(); reject(new ApiError(xhr.status, data || { detail: `Erreur ${xhr.status}` })); }
    };
    xhr.onerror = () => reject(new ApiError(0, { detail: "Téléversement interrompu (connexion perdue ?)." }));
    xhr.send(formData);
  });
}

export async function downloadPost(url, body, fallbackName) {
  const res = await fetch(url, { method: "POST", headers: { "X-CSRF-Token": csrf, "Content-Type": "application/json" }, body: JSON.stringify(body), credentials: "same-origin" });
  if (!res.ok) { const d = await res.json().catch(() => null); throw new ApiError(res.status, d); }
  const blob = await res.blob();
  const cd = res.headers.get("content-disposition") || "";
  const m = /filename\*?=(?:UTF-8'')?"?([^";]+)"?/i.exec(cd);
  const a = document.createElement("a");
  a.href = URL.createObjectURL(blob); a.download = m ? decodeURIComponent(m[1]) : fallbackName; document.body.append(a); a.click();
  setTimeout(() => { URL.revokeObjectURL(a.href); a.remove(); }, 1000);
}
export function downloadUrl(url) { const a = document.createElement("a"); a.href = url; a.download = ""; document.body.append(a); a.click(); a.remove(); }
