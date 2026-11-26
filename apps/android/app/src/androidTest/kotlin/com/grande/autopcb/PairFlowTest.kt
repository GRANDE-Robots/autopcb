/*
 * PairFlowTest.kt: drives the pair screen against a FakeServer: ready, token rejected, unreachable.
 *
 * Copyright: GRANDE Robots and Aaron John Sabu
 */

package com.grande.autopcb

import androidx.test.core.app.ActivityScenario
import androidx.test.espresso.Espresso.onView
import androidx.test.espresso.NoMatchingViewException
import androidx.test.espresso.action.ViewActions.click
import androidx.test.espresso.action.ViewActions.closeSoftKeyboard
import androidx.test.espresso.action.ViewActions.replaceText
import androidx.test.espresso.assertion.ViewAssertions.matches
import androidx.test.espresso.matcher.ViewMatchers.isDisplayed
import androidx.test.espresso.matcher.ViewMatchers.withId
import androidx.test.espresso.matcher.ViewMatchers.withText
import androidx.test.platform.app.InstrumentationRegistry
import org.junit.After
import org.junit.Assert.assertEquals
import org.junit.Assert.assertNull
import org.junit.Before
import org.junit.Test

class PairFlowTest {

    private val token = "test-token"
    private val server = FakeServer(token)
    private val store = PairingStore(InstrumentationRegistry.getInstrumentation().targetContext)

    @Before
    fun startServerAndForgetPairing() {
        server.start()
        store.clear()
    }

    @After
    fun stopServerAndForgetPairing() {
        server.stop()
        store.clear()
    }

    private fun pairWith(link: String) {
        onView(withId(R.id.link_input)).perform(replaceText(link), closeSoftKeyboard())
        onView(withId(R.id.pair_button)).perform(click())
    }

    private fun eventually(check: () -> Unit) {
        val deadline = System.currentTimeMillis() + 10_000
        while (true) {
            try {
                return check()
            } catch (error: NoMatchingViewException) {
                if (System.currentTimeMillis() > deadline) throw error
            } catch (error: AssertionError) {
                if (System.currentTimeMillis() > deadline) throw error
            }
            Thread.sleep(200)
        }
    }

    @Test
    fun readyServerOpensTheMainScreenAndStoresThePairing() {
        ActivityScenario.launch(PairActivity::class.java).use {
            pairWith("http://127.0.0.1:${server.port}/#token=$token")
            eventually { onView(withId(R.id.web_view)).check(matches(isDisplayed())) }
        }
        val saved = store.load()
        assertEquals(token, saved?.token)
        assertEquals(server.port, saved?.origin?.port)
    }

    @Test
    fun wrongTokenShowsRejectedMessageAndStoresNothing() {
        ActivityScenario.launch(PairActivity::class.java).use {
            pairWith("http://127.0.0.1:${server.port}/#token=wrong")
            eventually { onView(withId(R.id.pair_status)).check(matches(withText(R.string.status_token_rejected))) }
        }
        assertNull(store.load())
    }

    @Test
    fun stoppedServerShowsUnreachableMessageAndStoresNothing() {
        val port = server.port
        server.stop()
        ActivityScenario.launch(PairActivity::class.java).use {
            pairWith("http://127.0.0.1:$port/#token=$token")
            eventually { onView(withId(R.id.pair_status)).check(matches(withText(R.string.status_unreachable))) }
        }
        assertNull(store.load())
    }
}
