package com.sharecompute.worker;

import android.content.ContentProvider;
import android.content.ContentValues;
import android.content.Context;
import android.database.Cursor;
import android.database.MatrixCursor;
import android.net.Uri;
import android.os.ParcelFileDescriptor;
import android.provider.OpenableColumns;
import java.io.File;
import java.io.FileNotFoundException;
import java.io.IOException;

/**
 * Lets the share sheet read one exported log file and nothing else. This stands in for androidx's
 * FileProvider, which would add the AndroidX dependency to a project that has none. Not exported:
 * only the app the operator picks in the share sheet gets read access, through the URI grant.
 */
public final class LogProvider extends ContentProvider {
    static Uri uriFor(Context context, File file) {
        return new Uri.Builder().scheme("content").authority(context.getPackageName() + ".logs").appendPath("export").appendPath(file.getName()).build();
    }
    private File resolve(Uri uri) throws FileNotFoundException {
        try {
            File folder = new File(new File(getContext().getFilesDir(), "logs"), "export").getCanonicalFile();
            File file = new File(folder, String.valueOf(uri.getLastPathSegment())).getCanonicalFile();
            // A decoded segment could contain "../"; only a direct child of the export folder is served.
            if (!folder.equals(file.getParentFile()) || !file.isFile()) throw new FileNotFoundException("No such log");
            return file;
        } catch (IOException e) { throw new FileNotFoundException("No such log"); }
    }
    @Override public boolean onCreate() { return true; }
    @Override public ParcelFileDescriptor openFile(Uri uri, String mode) throws FileNotFoundException {
        if (!"r".equals(mode)) throw new SecurityException("Read only");
        return ParcelFileDescriptor.open(resolve(uri), ParcelFileDescriptor.MODE_READ_ONLY);
    }
    // Share targets ask for a name and size to label the attachment.
    @Override public Cursor query(Uri uri, String[] projection, String selection, String[] args, String sort) {
        try {
            File file = resolve(uri);
            String[] columns = projection != null ? projection : new String[]{OpenableColumns.DISPLAY_NAME, OpenableColumns.SIZE};
            Object[] row = new Object[columns.length];
            for (int i = 0; i < columns.length; i++) row[i] = OpenableColumns.DISPLAY_NAME.equals(columns[i]) ? file.getName() : OpenableColumns.SIZE.equals(columns[i]) ? (Object) file.length() : null;
            MatrixCursor cursor = new MatrixCursor(columns, 1); cursor.addRow(row); return cursor;
        } catch (FileNotFoundException e) { return null; }
    }
    @Override public String getType(Uri uri) { return "text/plain"; }
    @Override public Uri insert(Uri uri, ContentValues values) { throw new UnsupportedOperationException("Read only"); }
    @Override public int delete(Uri uri, String selection, String[] args) { throw new UnsupportedOperationException("Read only"); }
    @Override public int update(Uri uri, ContentValues values, String selection, String[] args) { throw new UnsupportedOperationException("Read only"); }
}
