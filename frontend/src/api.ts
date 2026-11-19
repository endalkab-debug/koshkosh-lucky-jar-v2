const API_BASE = (import.meta.env.VITE_API_BASE_URL || "").replace(/\/$/, "");
const initData = window.Telegram?.WebApp?.initData || "";

async function request<T>(path: string, options: RequestInit = {}): Promise<T> {
  const response = await fetch(`${API_BASE}/api/v1${path}`, {
    ...options,
    headers: {
      Authorization: `tma ${initData}`,
      ...(options.body instanceof FormData ? {} : { "Content-Type": "application/json" }),
      ...(options.headers || {}),
    },
  });
  const data = await response.json().catch(() => ({}));
  if (!response.ok) throw new Error(data.error || `Request failed (${response.status})`);
  return data as T;
}

export const api = {
  bootstrap: () => request<any>("/bootstrap"),
  board: () => request<any>("/board"),
  toggle: (number: number) => request<any>("/cart/numbers", { method: "POST", body: JSON.stringify({ number }) }),
  cancel: () => request<any>("/cart", { method: "DELETE" }),
  history: () => request<any>("/history"),
  checkoutStart: () => request<any>("/checkout/start", { method: "POST", body: JSON.stringify({}) }),
  checkoutName: (name: string) => request<any>("/checkout/name", { method: "POST", body: JSON.stringify({ name }) }),
  checkoutPhone: (phone: string) => request<any>("/checkout/phone", { method: "POST", body: JSON.stringify({ phone }) }),
  uploadReceipt: (orderId: string, file: File) => { const body = new FormData(); body.append("file", file); return request<any>(`/checkout/${encodeURIComponent(orderId)}/receipt`, { method: "POST", body }); },
  adminOrders: () => request<any>("/admin/orders"),
  adminReceiptUrl: (id: string) => request<any>(`/admin/orders/${encodeURIComponent(id)}/receipt-url`),
  decide: (id: string, decision: "approve" | "reject") => request<any>(`/admin/orders/${encodeURIComponent(id)}/decision`, { method: "POST", body: JSON.stringify({ decision }) }),
};
