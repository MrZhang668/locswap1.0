const TOKEN_KEY = 'locswap_token'

export const getToken = () => localStorage.getItem(TOKEN_KEY) || ''
export const setToken = (t) => localStorage.setItem(TOKEN_KEY, t)
export const clearToken = () => localStorage.removeItem(TOKEN_KEY)

export class ApiError extends Error {
  constructor(status, message) {
    super(message)
    this.status = status
  }
}

async function request(path, { method = 'GET', body, isForm } = {}) {
  const headers = {}
  const token = getToken()
  if (token) headers.Authorization = `Bearer ${token}`
  if (body && !isForm) headers['Content-Type'] = 'application/json'

  const res = await fetch(path, {
    method,
    headers,
    body: isForm ? body : body ? JSON.stringify(body) : undefined,
  })

  if (res.status === 204) return null
  const text = await res.text()
  let data = null
  try {
    data = text ? JSON.parse(text) : null
  } catch {
    data = text
  }
  if (!res.ok) {
    const detail = data?.detail
    const message =
      typeof detail === 'string'
        ? detail
        : Array.isArray(detail)
          ? detail.map((d) => d.msg).join('; ')
          : `Request failed (${res.status})`
    throw new ApiError(res.status, message)
  }
  return data
}

export const api = {
  // Auth / user
  register: (email, password) => request('/api/auth/register', { method: 'POST', body: { email, password } }),
  login: (email, password) => request('/api/auth/login', { method: 'POST', body: { email, password } }),
  me: () => request('/api/auth/me'),
  updateMe: (payload) => request('/api/auth/me', { method: 'PATCH', body: payload }),

  // Meta
  config: () => request('/api/meta/config'),

  // Home board: the same inventory, narrowed by explicit filters instead of a
  // sentence. `params` is a plain object; empty / null values are dropped so the
  // URL only carries filters the buyer actually set.
  browse: (params = {}) => {
    const qs = new URLSearchParams()
    Object.entries(params).forEach(([k, v]) => {
      if (v === null || v === undefined || v === '') return
      if (Array.isArray(v)) v.forEach((one) => qs.append(k, one))
      else qs.append(k, v)
    })
    return request(`/api/items/browse?${qs.toString()}`)
  },

  // Search
  chat: (message, history, clarifyRound) =>
    request('/api/search/chat', { method: 'POST', body: { message, history, clarify_round: clarifyRound } }),
  mapItems: () => request('/api/search/map'),

  // Listing
  uploadPhoto: (file) => {
    const form = new FormData()
    form.append('file', file)
    return request('/api/items/photo', { method: 'POST', body: form, isForm: true })
  },
  createItem: (payload) => request('/api/items', { method: 'POST', body: payload }),

  // Reservations / cart
  reserve: (itemId) => request(`/api/reservations/${itemId}`, { method: 'POST' }),
  cancel: (itemId) => request(`/api/reservations/${itemId}`, { method: 'DELETE' }),
  cart: () => request('/api/cart'),

  // Seller
  sellerItems: () => request('/api/seller/items'),
  // Seller sales advisor. The client sends no item_id and no history: which
  // listing a question is about is settled inside the conversation, and the
  // transcript itself is server-side state behind `thread_id`. Pass back the
  // thread_id the previous response returned to continue that conversation;
  // send null to start a fresh one.
  advisorChat: (message, threadId, photoPath) =>
    request('/api/seller/advisor/chat', {
      method: 'POST',
      body: { message, thread_id: threadId ?? null, photo_path: photoPath ?? null },
    }),
  // Publish the draft the seller reviewed. The backend re-runs the content
  // check on exactly this payload, so edits made after the draft was shown are
  // still checked.
  publishApprove: (payload) =>
    request('/api/seller/advisor/publish/approve', { method: 'POST', body: payload }),
  // Named conversations with the sales agent. The transcript lives on the
  // server; these only move the handle around.
  advisorThreads: () => request('/api/seller/advisor/threads'),
  newAdvisorThread: () => request('/api/seller/advisor/threads', { method: 'POST' }),
  advisorThread: (id) => request(`/api/seller/advisor/threads/${encodeURIComponent(id)}`),
  renameAdvisorThread: (id, title) =>
    request(`/api/seller/advisor/threads/${encodeURIComponent(id)}`, { method: 'PATCH', body: { title } }),
  deleteAdvisorThread: (id) =>
    request(`/api/seller/advisor/threads/${encodeURIComponent(id)}`, { method: 'DELETE' }),
  // 出售计划监控。这三个端点和对话里的 set_plan_watch 工具改的是**同一个字段**
  // ——界面上的开关和卖家用嘴说的话必须落到一处，否则其中一个在说假话。
  saleWatch: () => request('/api/seller/sale-watch'),
  setSaleWatch: (enabled) =>
    request('/api/seller/sale-watch', { method: 'PATCH', body: { enabled } }),
  salePlans: () => request('/api/seller/sale-plans'),
  setSalePlanWatch: (planId, watchEnabled) =>
    request(`/api/seller/sale-plans/${planId}`, {
      method: 'PATCH', body: { watch_enabled: watchEnabled },
    }),
  cancelSalePlan: (planId) =>
    request(`/api/seller/sale-plans/${planId}`, { method: 'DELETE' }),
  buyers: (itemId) => request(`/api/seller/items/${itemId}/buyers`),
  confirmDeal: (itemId, buyerId) =>
    request(`/api/seller/items/${itemId}/confirm`, { method: 'POST', body: { buyer_id: buyerId } }),
  delist: (itemId) => request(`/api/seller/items/${itemId}`, { method: 'DELETE' }),
  // Pause / resume. Separate from delist on purpose: one is reversible and one
  // deletes the row, and the seller-facing words for them are different too
  // (下架 vs 删除).
  setItemStatus: (itemId, target) =>
    request(`/api/seller/items/${itemId}/status`, { method: 'POST', body: { target } }),
  // Push this listing to one anonymized candidate card. Only the ordinal the
  // seller can see travels over the wire; the backend resolves it to a buyer.
  pushToBuyer: (itemId, cardId) =>
    request(`/api/seller/items/${itemId}/push`, { method: 'POST', body: { card_id: cardId } }),

  // Notifications
  notifications: () => request('/api/notifications'),
  unreadCount: () => request('/api/notifications/unread-count'),
  markRead: (id) => request(`/api/notifications/${id}/read`, { method: 'POST' }),
  markAllRead: () => request('/api/notifications/read-all', { method: 'POST' }),
  // Opening a push = mark read + record one item click (E6 tracking) — a separate thing from "mark read"
  clickNotification: (id) => request(`/api/notifications/${id}/click`, { method: 'POST' }),

  // Standing wants (things I'm waiting for)
  wants: () => request('/api/wants'),
  createWant: (text) => request('/api/wants', { method: 'POST', body: { text } }),
  subscribeWant: (id) => request(`/api/wants/${id}/subscribe`, { method: 'POST' }),
  closeWant: (id) => request(`/api/wants/${id}`, { method: 'DELETE' }),

  // One item, live (used by the pushed-listing card in Messages)
  item: (id) => request(`/api/items/${id}`),

  // Card click tracking. Never throws: tracking isn't important enough to interrupt the user
  clickItem: (id) =>
    request(`/api/items/${id}/click`, { method: 'POST' }).catch(() => null),
}

/** Client-side image compression: longest edge <= maxEdge; the backend compresses again (token-control rule: compress on upload). */
export function compressImage(file, maxEdge = 1024, quality = 0.85) {
  return new Promise((resolve, reject) => {
    const reader = new FileReader()
    reader.onerror = () => reject(new Error('图片读取失败'))
    reader.onload = () => {
      const img = new Image()
      img.onerror = () => reject(new Error('图片解码失败'))
      img.onload = () => {
        const scale = Math.min(1, maxEdge / Math.max(img.width, img.height))
        const canvas = document.createElement('canvas')
        canvas.width = Math.round(img.width * scale)
        canvas.height = Math.round(img.height * scale)
        const ctx = canvas.getContext('2d')
        ctx.fillStyle = '#fff'
        ctx.fillRect(0, 0, canvas.width, canvas.height)
        ctx.drawImage(img, 0, 0, canvas.width, canvas.height)
        canvas.toBlob(
          (blob) => {
            if (!blob) return reject(new Error('图片压缩失败'))
            resolve(new File([blob], 'upload.jpg', { type: 'image/jpeg' }))
          },
          'image/jpeg',
          quality,
        )
      }
      img.src = reader.result
    }
    reader.readAsDataURL(file)
  })
}
