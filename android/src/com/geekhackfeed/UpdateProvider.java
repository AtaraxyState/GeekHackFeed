package com.geekhackfeed;

import android.content.ContentProvider;
import android.content.ContentValues;
import android.database.Cursor;
import android.database.MatrixCursor;
import android.net.Uri;
import android.os.ParcelFileDescriptor;
import android.provider.OpenableColumns;

import java.io.File;
import java.io.FileNotFoundException;

/**
 * Hands the downloaded APK to the system package installer.
 *
 * Android forbids passing a file:// URI to another app, and the usual answer
 * is androidx FileProvider. This app has no AndroidX -- which is what keeps
 * the build down to a handful of SDK tool invocations -- so this is the same
 * idea in the fifty lines actually needed: serve exactly one file, read-only.
 */
public class UpdateProvider extends ContentProvider {

    public static final String AUTHORITY = "com.geekhackfeed.updates";
    private static final String MIME = "application/vnd.android.package-archive";

    public static Uri uriFor(File file) {
        return Uri.parse("content://" + AUTHORITY + "/" + file.getName());
    }

    /** Only ever resolves to the update we downloaded ourselves. */
    private File resolve(Uri uri) {
        String name = uri.getLastPathSegment();
        if (name == null || !UpdateManager.APK_NAME.equals(name) || getContext() == null) {
            return null;
        }
        return new File(getContext().getCacheDir(), UpdateManager.APK_NAME);
    }

    @Override
    public boolean onCreate() {
        return true;
    }

    @Override
    public ParcelFileDescriptor openFile(Uri uri, String mode) throws FileNotFoundException {
        File file = resolve(uri);
        if (file == null || !file.exists()) {
            throw new FileNotFoundException(String.valueOf(uri));
        }
        return ParcelFileDescriptor.open(file, ParcelFileDescriptor.MODE_READ_ONLY);
    }

    @Override
    public Cursor query(Uri uri, String[] projection, String selection,
                        String[] selectionArgs, String sortOrder) {
        File file = resolve(uri);
        if (file == null || !file.exists()) {
            return null;
        }
        // The installer reads the name and size before it will open anything.
        MatrixCursor cursor = new MatrixCursor(
                new String[]{OpenableColumns.DISPLAY_NAME, OpenableColumns.SIZE});
        cursor.addRow(new Object[]{file.getName(), file.length()});
        return cursor;
    }

    @Override
    public String getType(Uri uri) {
        return MIME;
    }

    @Override
    public Uri insert(Uri uri, ContentValues values) {
        throw new UnsupportedOperationException("read-only");
    }

    @Override
    public int delete(Uri uri, String selection, String[] selectionArgs) {
        throw new UnsupportedOperationException("read-only");
    }

    @Override
    public int update(Uri uri, ContentValues values, String selection, String[] selectionArgs) {
        throw new UnsupportedOperationException("read-only");
    }
}
