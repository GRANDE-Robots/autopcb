/*
 * OriginLockedClient.kt: keeps a WebView on the paired origin and reports main-frame load errors.
 *
 * Copyright: GRANDE Robots and Aaron John Sabu
 */

package com.grande.autopcb

import android.webkit.WebResourceError
import android.webkit.WebResourceRequest
import android.webkit.WebView
import android.webkit.WebViewClient

/**
 * A WebViewClient that never leaves [origin].
 *
 * Inputs: the paired [origin] and [onMainFrameError], called when the page itself
 * fails to load.
 */
class OriginLockedClient(
    private val origin: Origin,
    private val onMainFrameError: () -> Unit,
) : WebViewClient() {

    /** Refuses, without opening anything else, every navigation to another origin. */
    override fun shouldOverrideUrlLoading(view: WebView, request: WebResourceRequest): Boolean =
        !origin.contains(request.url.toString())

    /** Reports a failed load of the main frame; failed sub-resources are ignored. */
    override fun onReceivedError(view: WebView, request: WebResourceRequest, error: WebResourceError) {
        if (request.isForMainFrame) onMainFrameError()
    }
}
