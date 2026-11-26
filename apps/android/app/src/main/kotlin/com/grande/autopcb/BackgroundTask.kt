/*
 * BackgroundTask.kt: runs blocking work off the main thread and hands the result back to it.
 *
 * Copyright: GRANDE Robots and Aaron John Sabu
 */

package com.grande.autopcb

import android.app.Activity
import java.util.concurrent.ExecutorService
import java.util.concurrent.Executors

/**
 * Runs one job at a time on a worker thread for an [activity].
 *
 * Inputs: the activity whose main thread receives results.
 */
class BackgroundTask(private val activity: Activity) {

    private val executor: ExecutorService = Executors.newSingleThreadExecutor()

    /**
     * Runs [work] on the worker thread, then [done] on the main thread with its result.
     * [done] is skipped when the activity has been destroyed meanwhile.
     */
    fun <T> run(work: () -> T, done: (T) -> Unit) {
        executor.execute {
            val result = work()
            activity.runOnUiThread { if (!activity.isDestroyed) done(result) }
        }
    }

    /** Stops accepting jobs; call from the activity's onDestroy. */
    fun shutdown() {
        executor.shutdown()
    }
}
