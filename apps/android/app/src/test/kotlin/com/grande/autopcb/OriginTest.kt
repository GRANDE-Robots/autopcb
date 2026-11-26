/*
 * OriginTest.kt: checks which URLs count as the paired origin.
 *
 * Copyright: GRANDE Robots and Aaron John Sabu
 */

package com.grande.autopcb

import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Test

class OriginTest {

    private val origin = Origin("http", "pc.local", 8765)

    @Test
    fun containsPagesOnTheSameSchemeHostAndPort() {
        assertTrue(origin.contains("http://pc.local:8765/"))
        assertTrue(origin.contains("http://PC.local:8765/api/info?x=1#frag"))
    }

    @Test
    fun excludesOtherHostPortOrScheme() {
        assertFalse(origin.contains("http://evil.example:8765/"))
        assertFalse(origin.contains("http://pc.local:9999/"))
        assertFalse(origin.contains("https://pc.local:8765/"))
        assertFalse(origin.contains("http://pc.local.evil.example:8765/"))
        assertFalse(origin.contains("http://pc.local:8765@evil.example/"))
    }

    @Test
    fun excludesNonUrlsAndOtherSchemes() {
        assertFalse(origin.contains("javascript:alert(1)"))
        assertFalse(origin.contains("intent://x#Intent;end"))
        assertFalse(origin.contains("not a url"))
    }

    @Test
    fun missingPortMeansPort80() {
        assertEquals(Origin("http", "h", 80), Origin.from("http://h/x"))
        assertNull(Origin.from("https://h/x"))
    }
}
