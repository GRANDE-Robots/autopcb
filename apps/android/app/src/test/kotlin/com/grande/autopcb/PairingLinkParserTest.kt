/*
 * PairingLinkParserTest.kt: checks which pairing links the parser accepts and rejects.
 *
 * Copyright: GRANDE Robots and Aaron John Sabu
 */

package com.grande.autopcb

import org.junit.Assert.assertEquals
import org.junit.Assert.assertNull
import org.junit.Test

class PairingLinkParserTest {

    private val parse = PairingLinkParser()

    @Test
    fun readsOriginAndTokenFromServerLink() {
        val link = parse("http://192.168.1.20:8765/#token=abc-DEF_123")
        assertEquals(Origin("http", "192.168.1.20", 8765), link?.origin)
        assertEquals("abc-DEF_123", link?.token)
    }

    @Test
    fun ignoresSurroundingWhitespace() {
        assertEquals("abc", parse("  http://h:1/#token=abc\n")?.token)
    }

    @Test
    fun rejectsLinkWithoutToken() {
        assertNull(parse("http://h:1/"))
        assertNull(parse("http://h:1/#"))
        assertNull(parse("http://h:1/#token="))
        assertNull(parse("http://h:1/#other=abc"))
    }

    @Test
    fun usesPort80WhenPortIsMissing() {
        assertEquals(Origin("http", "h", 80), parse("http://h/#token=abc")?.origin)
    }

    @Test
    fun rejectsSchemeOtherThanHttp() {
        assertNull(parse("https://h:1/#token=abc"))
        assertNull(parse("ftp://h:1/#token=abc"))
        assertNull(parse("h:1/#token=abc"))
    }

    @Test
    fun readsIpv6Host() {
        assertEquals(Origin("http", "[::1]", 8765), parse("http://[::1]:8765/#token=abc")?.origin)
    }

    @Test
    fun rejectsTextAfterTheLink() {
        assertNull(parse("http://h:1/#token=abc and more"))
        assertNull(parse("http://h:1/#token=abc http://h:1/#token=def"))
    }

    @Test
    fun rejectsPathAndQueryToken() {
        assertNull(parse("http://h:1/other#token=abc"))
        assertNull(parse("http://h:1/?token=abc"))
    }

    @Test
    fun rejectsEmptyAndGarbageText() {
        assertNull(parse(""))
        assertNull(parse("not a link"))
    }

    @Test
    fun loginUrlPutsTheTokenInTheFragment() {
        assertEquals("http://h:1/#token=abc", parse("http://h:1/#token=abc")?.loginUrl)
    }

    @Test
    fun toStringHidesTheToken() {
        assertEquals("PairingLink(http://h:1)", parse("http://h:1/#token=secret")?.toString())
    }
}
