async function readError(res) {
  const text = await res.text()
  try {
    const data = JSON.parse(text)
    const detail = data.detail
    if (detail && typeof detail === 'object') {
      return detail.原因 || detail.错误 || JSON.stringify(detail)
    }
    return typeof detail === 'string' ? detail : text
  } catch {
    return text
  }
}

export async function getJson(url) {
  const res = await fetch(url)
  if (!res.ok) throw new Error(await readError(res))
  return res.json()
}

export async function postJson(url, body) {
  const res = await fetch(url, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
  })
  if (!res.ok) throw new Error(await readError(res))
  return res.json()
}
