/*
 * PairActivity.kt: the pair screen: scan or paste a link, check the server, store the pairing.
 *
 * Copyright: GRANDE Robots and Aaron John Sabu
 */

package com.grande.autopcb

import android.content.Intent
import android.os.Bundle
import android.widget.Button
import android.widget.EditText
import android.widget.TextView
import androidx.appcompat.app.AppCompatActivity
import com.journeyapps.barcodescanner.ScanContract
import com.journeyapps.barcodescanner.ScanOptions

/** Pairs the app with an `autopcb serve` instance. */
class PairActivity : AppCompatActivity() {

    private val parse = PairingLinkParser()
    private val message = StatusMessage()
    private val infoClient = InfoClient()
    private lateinit var store: PairingStore
    private lateinit var background: BackgroundTask
    private lateinit var linkInput: EditText
    private lateinit var statusText: TextView
    private lateinit var pairButton: Button

    private val scanner = registerForActivityResult(ScanContract()) { result ->
        result.contents?.let {
            linkInput.setText(it)
            pair()
        }
    }

    /** Builds the screen and wires the scan and Pair buttons. */
    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        setContentView(R.layout.activity_pair)
        store = PairingStore(this)
        background = BackgroundTask(this)
        linkInput = findViewById(R.id.link_input)
        statusText = findViewById(R.id.pair_status)
        pairButton = findViewById(R.id.pair_button)
        findViewById<Button>(R.id.scan_button).setOnClickListener {
            scanner.launch(
                ScanOptions()
                    .setDesiredBarcodeFormats(ScanOptions.QR_CODE)
                    .setPrompt(getString(R.string.scan_prompt))
                    .setBeepEnabled(false)
                    .setOrientationLocked(false),
            )
        }
        pairButton.setOnClickListener { pair() }
    }

    /** Stops the worker thread. */
    override fun onDestroy() {
        background.shutdown()
        super.onDestroy()
    }

    private fun pair() {
        val link = parse(linkInput.text.toString())
        if (link == null) {
            statusText.setText(R.string.status_bad_link)
            return
        }
        pairButton.isEnabled = false
        statusText.setText(R.string.status_checking)
        background.run({ infoClient(link) }) { status ->
            pairButton.isEnabled = true
            statusText.setText(message(status))
            if (status == ServerStatus.READY) {
                store.save(link)
                startActivity(Intent(this, MainActivity::class.java))
                finish()
            }
        }
    }
}
