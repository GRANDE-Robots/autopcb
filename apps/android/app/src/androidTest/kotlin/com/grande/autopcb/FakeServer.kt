/*
 * FakeServer.kt: an in-process AutoPCB stand-in that serves /api/info and a trivial page.
 *
 * Copyright: GRANDE Robots and Aaron John Sabu
 */

package com.grande.autopcb

import java.io.IOException
import java.net.InetAddress
import java.net.ServerSocket
import java.net.Socket
import kotlin.concurrent.thread

/**
 * Serves `/api/info`, authorized only for `Bearer [token]`, and a page at every other
 * path. Plain sockets are used because Android has no `com.sun.net.httpserver`.
 *
 * Inputs: the [token] the server accepts. It listens on 127.0.0.1 on a free port.
 */
class FakeServer(private val token: String) {

    private val socket = ServerSocket(0, 8, InetAddress.getByName("127.0.0.1"))

    /** The port the server listens on. */
    val port: Int get() = socket.localPort

    /** Starts answering requests on a background thread. */
    fun start() {
        thread(isDaemon = true) {
            while (!socket.isClosed) {
                try {
                    socket.accept().use { answer(it) }
                } catch (_: IOException) {
                    // The socket was closed by stop(), or the client went away.
                }
            }
        }
    }

    /** Stops serving and frees the port. */
    fun stop() = socket.close()

    private fun answer(client: Socket) {
        val reader = client.getInputStream().bufferedReader()
        val path = reader.readLine().split(' ')[1]
        val headers = generateSequence { reader.readLine() }
            .takeWhile { it.isNotEmpty() }
            .associate { it.substringBefore(':').lowercase() to it.substringAfter(':').trim() }
        val (type, body) = if (path == "/api/info") {
            val authorized = headers["authorization"] == "Bearer $token"
            "application/json" to """{"product":"AutoPCB","version":"test","api":1,"authorized":$authorized}"""
        } else {
            "text/html" to "<html><body>AutoPCB</body></html>"
        }
        val bytes = body.toByteArray()
        client.getOutputStream().apply {
            write("HTTP/1.1 200 OK\r\nContent-Type: $type\r\nContent-Length: ${bytes.size}\r\nConnection: close\r\n\r\n".toByteArray())
            write(bytes)
            flush()
        }
    }
}
