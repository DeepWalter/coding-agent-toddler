/**
 * Clipboard writes for the app's copy buttons.
 *
 * The clipboard API needs a secure context, and this app is reachable over
 * the LAN — fall back to the textarea trick rather than failing silently on
 * plain HTTP.
 */
export async function writeClipboard(text: string): Promise<void> {
  if (navigator.clipboard?.writeText) {
    await navigator.clipboard.writeText(text)
    return
  }
  const scratch = document.createElement('textarea')
  scratch.value = text // never innerHTML — the text is the model's
  scratch.setAttribute('readonly', '')
  scratch.style.position = 'fixed'
  scratch.style.top = '-1000px'
  document.body.appendChild(scratch)
  scratch.select()
  const ok = document.execCommand('copy')
  scratch.remove()
  if (!ok) throw new Error('the browser refused the copy')
}
