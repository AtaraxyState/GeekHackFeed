package com.geekhackfeed;

import android.app.Activity;
import android.app.AlertDialog;
import android.content.Context;
import android.content.DialogInterface;
import android.content.Intent;
import android.content.SharedPreferences;
import android.content.pm.PackageInfo;
import android.net.Uri;
import android.os.Build;
import android.os.Handler;
import android.os.Looper;
import android.provider.Settings;
import android.view.Gravity;
import android.widget.LinearLayout;
import android.widget.ProgressBar;
import android.widget.TextView;
import android.widget.Toast;

import org.json.JSONArray;
import org.json.JSONObject;

import java.io.File;
import java.io.FileOutputStream;
import java.io.InputStream;
import java.io.OutputStream;
import java.net.HttpURLConnection;
import java.net.URL;

/**
 * Checks GitHub Releases for a newer build and installs it.
 *
 * Releases are published by .github/workflows/release.yml, which derives the
 * version code from the tag with the same formula used here, so comparing a
 * tag against the installed versionCode is exact rather than a string guess.
 */
public class UpdateManager {

    public static final String APK_NAME = "update.apk";

    private static final String REPO = "AtaraxyState/GeekHackFeed";
    private static final String LATEST_URL =
            "https://api.github.com/repos/" + REPO + "/releases/latest";
    private static final String PREFS = "geekhack_feed";
    private static final String KEY_LAST_CHECK = "last_update_check";
    private static final String KEY_SKIPPED = "skipped_version";
    /** Background checks are throttled to this; the menu item ignores it. */
    private static final long CHECK_INTERVAL_MS = 6 * 60 * 60 * 1000L;

    private UpdateManager() {
    }

    /** A published release, already resolved to something installable. */
    public static class Release {
        String tag;
        String name;
        String notes;
        String apkUrl;
        long size;
        int versionCode;
    }

    // ------------------------------------------------------------ versioning

    /** 1.2.3 -> 10203. Mirrors semver_to_code() in build.py. */
    static int versionCodeOf(String tag) {
        if (tag == null) {
            return 0;
        }
        String cleaned = tag.trim();
        while (cleaned.startsWith("v") || cleaned.startsWith("V")) {
            cleaned = cleaned.substring(1);
        }
        int dash = cleaned.indexOf('-');
        if (dash >= 0) {
            cleaned = cleaned.substring(0, dash);
        }
        String[] parts = cleaned.split("\\.");
        int[] numbers = new int[]{0, 0, 0};
        for (int i = 0; i < 3 && i < parts.length; i++) {
            try {
                numbers[i] = Integer.parseInt(parts[i]);
            } catch (NumberFormatException e) {
                return 0;
            }
        }
        return numbers[0] * 10000 + numbers[1] * 100 + numbers[2];
    }

    @SuppressWarnings("deprecation")
    static int installedVersionCode(Context context) {
        try {
            PackageInfo info = context.getPackageManager()
                    .getPackageInfo(context.getPackageName(), 0);
            return info.versionCode;
        } catch (Exception e) {
            return 0;
        }
    }

    // ----------------------------------------------------------------- check

    public interface Callback {
        /** release is null when there is nothing newer. */
        void onResult(Release release, String error);
    }

    /**
     * @param force true for the menu item, false for the throttled startup check
     */
    public static void check(final Context context, final boolean force,
                             final Callback callback) {
        final SharedPreferences prefs =
                context.getSharedPreferences(PREFS, Context.MODE_PRIVATE);
        long since = System.currentTimeMillis() - prefs.getLong(KEY_LAST_CHECK, 0);
        if (!force && since < CHECK_INTERVAL_MS) {
            return;
        }

        new Thread(new Runnable() {
            @Override
            public void run() {
                String error = null;
                Release release = null;
                try {
                    release = fetchLatest();
                    prefs.edit().putLong(KEY_LAST_CHECK, System.currentTimeMillis()).apply();

                    if (release != null) {
                        int installed = installedVersionCode(context);
                        boolean newer = release.versionCode > installed;
                        boolean skipped = !force
                                && release.tag.equals(prefs.getString(KEY_SKIPPED, null));
                        if (!newer || skipped) {
                            release = null;
                        }
                    }
                } catch (Exception e) {
                    error = e.getMessage() == null ? e.toString() : e.getMessage();
                }
                post(callback, release, error);
            }
        }).start();
    }

    private static void post(final Callback callback, final Release release,
                             final String error) {
        new Handler(Looper.getMainLooper()).post(new Runnable() {
            @Override
            public void run() {
                callback.onResult(release, error);
            }
        });
    }

    private static Release fetchLatest() throws Exception {
        HttpURLConnection conn = (HttpURLConnection) new URL(LATEST_URL).openConnection();
        conn.setConnectTimeout(10_000);
        conn.setReadTimeout(20_000);
        conn.setRequestProperty("Accept", "application/vnd.github+json");
        conn.setRequestProperty("User-Agent", "geekhack-feed-android");
        try {
            int status = conn.getResponseCode();
            if (status == 404) {
                return null; // no releases published yet
            }
            if (status != 200) {
                throw new Exception("GitHub returned HTTP " + status);
            }
            JSONObject json = new JSONObject(readAll(conn.getInputStream()));

            Release release = new Release();
            release.tag = json.optString("tag_name", "");
            release.name = json.optString("name", release.tag);
            release.notes = json.optString("body", "");
            release.versionCode = versionCodeOf(release.tag);

            JSONArray assets = json.optJSONArray("assets");
            for (int i = 0; assets != null && i < assets.length(); i++) {
                JSONObject asset = assets.getJSONObject(i);
                if (asset.optString("name", "").endsWith(".apk")) {
                    release.apkUrl = asset.optString("browser_download_url", null);
                    release.size = asset.optLong("size", 0);
                    break;
                }
            }
            return release.apkUrl == null ? null : release;
        } finally {
            conn.disconnect();
        }
    }

    private static String readAll(InputStream in) throws Exception {
        java.io.ByteArrayOutputStream buffer = new java.io.ByteArrayOutputStream();
        byte[] chunk = new byte[8192];
        int read;
        while ((read = in.read(chunk)) != -1) {
            buffer.write(chunk, 0, read);
        }
        in.close();
        return buffer.toString("UTF-8");
    }

    // ---------------------------------------------------------------- prompt

    public static void promptAndInstall(final Activity activity, final Release release) {
        String notes = release.notes == null ? "" : release.notes.trim();
        if (notes.length() > 600) {
            notes = notes.substring(0, 600) + "…";
        }
        String message = "Installed: " + versionName(activity)
                + "\nAvailable: " + release.tag;
        if (release.size > 0) {
            message += "  (" + (release.size / 1024) + " KB)";
        }
        if (!notes.isEmpty()) {
            message += "\n\n" + notes;
        }

        new AlertDialog.Builder(activity)
                .setTitle("Update available")
                .setMessage(message)
                .setPositiveButton("Install", new DialogInterface.OnClickListener() {
                    @Override
                    public void onClick(DialogInterface dialog, int which) {
                        startDownload(activity, release);
                    }
                })
                .setNegativeButton("Later", null)
                .setNeutralButton("Skip this one", new DialogInterface.OnClickListener() {
                    @Override
                    public void onClick(DialogInterface dialog, int which) {
                        activity.getSharedPreferences(PREFS, Context.MODE_PRIVATE)
                                .edit().putString(KEY_SKIPPED, release.tag).apply();
                    }
                })
                .show();
    }

    static String versionName(Context context) {
        try {
            return context.getPackageManager()
                    .getPackageInfo(context.getPackageName(), 0).versionName;
        } catch (Exception e) {
            return "?";
        }
    }

    // -------------------------------------------------------------- download

    private static void startDownload(final Activity activity, final Release release) {
        final LinearLayout box = new LinearLayout(activity);
        box.setOrientation(LinearLayout.VERTICAL);
        int pad = (int) (22 * activity.getResources().getDisplayMetrics().density);
        box.setPadding(pad, pad, pad, pad);

        final TextView label = new TextView(activity);
        label.setText("Downloading " + release.tag + "…");
        label.setGravity(Gravity.CENTER_HORIZONTAL);
        box.addView(label);

        final ProgressBar bar = new ProgressBar(activity, null,
                android.R.attr.progressBarStyleHorizontal);
        bar.setMax(100);
        bar.setIndeterminate(release.size <= 0);
        box.addView(bar);

        final AlertDialog dialog = new AlertDialog.Builder(activity)
                .setView(box)
                .setCancelable(false)
                .create();
        dialog.show();

        new Thread(new Runnable() {
            @Override
            public void run() {
                final File target = new File(activity.getCacheDir(), APK_NAME);
                String error = null;
                HttpURLConnection conn = null;
                try {
                    conn = (HttpURLConnection) new URL(release.apkUrl).openConnection();
                    conn.setConnectTimeout(15_000);
                    conn.setReadTimeout(60_000);
                    conn.setInstanceFollowRedirects(true);
                    conn.setRequestProperty("User-Agent", "geekhack-feed-android");
                    if (conn.getResponseCode() != 200) {
                        throw new Exception("HTTP " + conn.getResponseCode());
                    }
                    long total = release.size > 0 ? release.size : conn.getContentLength();

                    InputStream in = conn.getInputStream();
                    OutputStream out = new FileOutputStream(target);
                    byte[] chunk = new byte[16384];
                    long done = 0;
                    int read;
                    int lastPercent = -1;
                    while ((read = in.read(chunk)) != -1) {
                        out.write(chunk, 0, read);
                        done += read;
                        if (total > 0) {
                            final int percent = (int) (done * 100 / total);
                            if (percent != lastPercent) {
                                lastPercent = percent;
                                bar.post(new Runnable() {
                                    @Override
                                    public void run() {
                                        bar.setProgress(percent);
                                    }
                                });
                            }
                        }
                    }
                    out.close();
                    in.close();
                } catch (Exception e) {
                    error = e.getMessage() == null ? e.toString() : e.getMessage();
                } finally {
                    if (conn != null) {
                        conn.disconnect();
                    }
                }

                final String failure = error;
                new Handler(Looper.getMainLooper()).post(new Runnable() {
                    @Override
                    public void run() {
                        dialog.dismiss();
                        if (failure != null) {
                            target.delete();
                            new AlertDialog.Builder(activity)
                                    .setTitle("Download failed")
                                    .setMessage(failure)
                                    .setPositiveButton("OK", null)
                                    .show();
                        } else {
                            install(activity, target);
                        }
                    }
                });
            }
        }).start();
    }

    // --------------------------------------------------------------- install

    private static void install(final Activity activity, File apk) {
        // Since API 26 an app must hold this permission per-app, and the user
        // grants it in Settings rather than through a runtime prompt.
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O
                && !activity.getPackageManager().canRequestPackageInstalls()) {
            new AlertDialog.Builder(activity)
                    .setTitle("One-time permission")
                    .setMessage("Android needs to let this app install updates. "
                            + "Enable it on the next screen, then tap Install again.")
                    .setPositiveButton("Open settings", new DialogInterface.OnClickListener() {
                        @Override
                        public void onClick(DialogInterface dialog, int which) {
                            try {
                                activity.startActivity(new Intent(
                                        Settings.ACTION_MANAGE_UNKNOWN_APP_SOURCES,
                                        Uri.parse("package:" + activity.getPackageName())));
                            } catch (Exception e) {
                                Toast.makeText(activity,
                                        "Could not open that settings screen",
                                        Toast.LENGTH_LONG).show();
                            }
                        }
                    })
                    .setNegativeButton("Cancel", null)
                    .show();
            return;
        }

        Intent intent = new Intent(Intent.ACTION_VIEW);
        intent.setDataAndType(UpdateProvider.uriFor(apk),
                "application/vnd.android.package-archive");
        intent.addFlags(Intent.FLAG_GRANT_READ_URI_PERMISSION
                | Intent.FLAG_ACTIVITY_NEW_TASK);
        try {
            activity.startActivity(intent);
        } catch (Exception e) {
            Toast.makeText(activity, "No installer available: " + e.getMessage(),
                    Toast.LENGTH_LONG).show();
        }
    }
}
