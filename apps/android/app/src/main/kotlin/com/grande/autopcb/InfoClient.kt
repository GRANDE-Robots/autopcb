/*
 * InfoClient.kt: asks a server's /api/info who it is and whether the token is right.
 *
 * Copyright: GRANDE Robots and Aaron John Sabu
 */

package com.grande.autopcb

import java.io.IOException
import java.net.HttpURLConnection
import java.net.URL

/**
 * Calls `GET {origin}/api/info` with the bearer token.
 *
 * Inputs: [timeoutMs], the connect and read timeout in milliseconds.
 */
class InfoClient(
    private val timeoutMs: Int = 4000,
    private val classify: ServerStatusClassifier = ServerStatusClassifier(),
) {

    /**
     * Asks one server about itself. Blocks, so call it off the main thread.
     *
     * Outputs: UNREACHABLE on any IOException or timeout, NOT_AUTOPCB when the status
     * is not 200, otherwise the classified reply.
     */
    operator fun invoke(link: PairingLink): ServerStatus {
        val connection = URL("${link.origin}/api/info").openConnection() as HttpURLConnection
        return try {
            connection.connectTimeout = timeoutMs
            connection.readTimeout = timeoutMs
            connection.setRequestProperty("Authorization", "Bearer ${link.token}")
            if (connection.responseCode != HttpURLConnection.HTTP_OK) {
                ServerStatus.NOT_AUTOPCB
            } else {
                classify(connection.inputStream.use { it.readBytes().decodeToString() })
            }
        } catch (_: IOException) {
            ServerStatus.UNREACHABLE
        } finally {
            connection.disconnect()
        }
    }
}
