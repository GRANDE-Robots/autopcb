/*
 * ServerStatusClassifierTest.kt: checks that every /api/info reply maps to the right ServerStatus.
 *
 * Copyright: GRANDE Robots and Aaron John Sabu
 */

package com.grande.autopcb

import org.junit.Assert.assertEquals
import org.junit.Test

class ServerStatusClassifierTest {

    private val classify = ServerStatusClassifier()

    private fun info(product: String = "AutoPCB", api: Int = 1, authorized: Boolean = true) =
        """{"product":"$product","version":"1.2.3","api":$api,"authorized":$authorized}"""

    @Test
    fun missingBodyIsUnreachable() {
        assertEquals(ServerStatus.UNREACHABLE, classify(null))
    }

    @Test
    fun otherProductIsNotAutopcb() {
        assertEquals(ServerStatus.NOT_AUTOPCB, classify(info(product = "Other")))
    }

    @Test
    fun otherApiLevelIsNotAutopcb() {
        assertEquals(ServerStatus.NOT_AUTOPCB, classify(info(api = 2)))
    }

    @Test
    fun nonJsonBodyIsNotAutopcb() {
        assertEquals(ServerStatus.NOT_AUTOPCB, classify("<html>hello</html>"))
        assertEquals(ServerStatus.NOT_AUTOPCB, classify(""))
    }

    @Test
    fun unauthorizedIsTokenRejected() {
        assertEquals(ServerStatus.TOKEN_REJECTED, classify(info(authorized = false)))
    }

    @Test
    fun missingAuthorizedIsTokenRejected() {
        assertEquals(ServerStatus.TOKEN_REJECTED, classify("""{"product":"AutoPCB","api":1}"""))
    }

    @Test
    fun authorizedAutopcbApi1IsReady() {
        assertEquals(ServerStatus.READY, classify(info()))
    }
}
