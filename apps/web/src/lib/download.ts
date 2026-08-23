/**
 * Handing the browser a file.
 *
 * One function, in its own module because the mechanics are fiddly in ways that
 * fail quietly: an object URL that is never revoked pins the whole blob in
 * memory for the life of the tab, and an anchor that is clicked while detached
 * from the document does nothing at all in some browsers.
 *
 * Nothing here knows what it is saving. *What* goes into a file is a question
 * about the data and is answered by the module that owns it —
 * `lib/backtestExport.ts` is the one caller today.
 */

/** Two spaces. These files are meant to be opened and read, not only parsed. */
const INDENT = 2

/**
 * Save `value` as a `.json` file the browser offers to the reader.
 *
 * `application/json` rather than `application/octet-stream`: the type is the
 * truth about the bytes, and a browser that offers to open it in a JSON viewer
 * is a feature here — nothing in the file is executable.
 */
export function saveJson(filename: string, value: unknown): void {
  const url = URL.createObjectURL(
    new Blob([JSON.stringify(value, null, INDENT)], { type: 'application/json' }),
  )
  try {
    const anchor = document.createElement('a')
    anchor.href = url
    anchor.download = filename
    // In the document before the click and out of it after: a detached anchor's
    // click is a no-op in Firefox, and one left behind is a dead node per
    // download.
    document.body.appendChild(anchor)
    anchor.click()
    anchor.remove()
  } finally {
    // Deferred rather than revoked inline. The click starts the download
    // synchronously, but revoking in the same task has historically cancelled
    // it in Safari; a macrotask later the browser holds its own reference. In
    // a `finally` so a throw between creating the URL and clicking it still
    // releases the blob.
    setTimeout(() => URL.revokeObjectURL(url), 0)
  }
}
