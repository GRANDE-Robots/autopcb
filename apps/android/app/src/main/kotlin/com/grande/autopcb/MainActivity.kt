/*
 * MainActivity.kt: the main screen: the paired page in a locked WebView plus connection overlays.
 *
 * Copyright: GRANDE Robots and Aaron John Sabu
 */

package com.grande.autopcb

import android.annotation.SuppressLint
import android.content.Intent
import android.os.Bundle
import android.view.Menu
import android.view.MenuItem
import android.view.View
import android.webkit.WebView
import android.widget.Button
import android.widget.TextView
import androidx.appcompat.app.AppCompatActivity

/** Shows the paired AutoPCB page, or an overlay when the server cannot be used. */
class MainActivity : AppCompatActivity() {

    private val message = StatusMessage()
    private val infoClient = InfoClient()
    private lateinit var store: PairingStore
    private lateinit var background: BackgroundTask
    private lateinit var link: PairingLink
    private lateinit var webView: WebView
    private lateinit var overlay: View
    private lateinit var overlayText: TextView
    private lateinit var retryButton: Button
    private var reloadNeeded = false

    /** Opens the pair screen when nothing is stored, otherwise loads the paired page. */
    @SuppressLint("SetJavaScriptEnabled")
    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        store = PairingStore(this)
        val stored = store.load()
        if (stored == null) {
            startActivity(Intent(this, PairActivity::class.java))
            finish()
            return
        }
        link = stored
        setContentView(R.layout.activity_main)
        background = BackgroundTask(this)
        webView = findViewById(R.id.web_view)
        overlay = findViewById(R.id.overlay)
        overlayText = findViewById(R.id.overlay_text)
        retryButton = findViewById(R.id.retry_button)
        retryButton.setOnClickListener { check() }
        findViewById<Button>(R.id.pair_again_button).setOnClickListener { pairAgain() }

        WebView.setWebContentsDebuggingEnabled(false)
        webView.settings.apply {
            javaScriptEnabled = true
            domStorageEnabled = true
            allowFileAccess = false
            allowContentAccess = false
        }
        webView.webViewClient = OriginLockedClient(link.origin) { showOverlay(R.string.status_unreachable, true) }
        webView.loadUrl(link.loginUrl)
    }

    /** Checks the server each time the app comes to the front. */
    override fun onResume() {
        super.onResume()
        if (::link.isInitialized) check()
    }

    /** Releases the WebView and the worker thread. */
    override fun onDestroy() {
        if (::link.isInitialized) {
            background.shutdown()
            webView.destroy()
        }
        super.onDestroy()
    }

    /** Adds the Forget this computer item. */
    override fun onCreateOptionsMenu(menu: Menu): Boolean {
        menuInflater.inflate(R.menu.main, menu)
        return true
    }

    /** Clears the pairing and returns to the pair screen. */
    override fun onOptionsItemSelected(item: MenuItem): Boolean {
        if (item.itemId != R.id.forget) return super.onOptionsItemSelected(item)
        store.clear()
        pairAgain()
        return true
    }

    private fun check() {
        background.run({ infoClient(link) }) { status ->
            if (status == ServerStatus.READY) {
                overlay.visibility = View.GONE
                if (reloadNeeded) webView.loadUrl(link.loginUrl)
                reloadNeeded = false
            } else {
                showOverlay(message(status), status == ServerStatus.UNREACHABLE)
            }
        }
    }

    private fun showOverlay(text: Int, canRetry: Boolean) {
        reloadNeeded = true
        overlayText.setText(text)
        retryButton.visibility = if (canRetry) View.VISIBLE else View.GONE
        overlay.visibility = View.VISIBLE
    }

    private fun pairAgain() {
        startActivity(Intent(this, PairActivity::class.java))
        finish()
    }
}
