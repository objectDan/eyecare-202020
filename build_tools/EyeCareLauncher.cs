using System;
using System.Diagnostics;
using System.IO;
using System.Text;
using System.Windows.Forms;

internal static class EyeCareLauncher
{
    private const string AppScriptName = "eyecare_202020_v9.pyw";

    [STAThread]
    private static int Main(string[] args)
    {
        string baseDir = AppDomain.CurrentDomain.BaseDirectory;
        string pythonwPath = Path.Combine(baseDir, "runtime", "pythonw.exe");
        string scriptPath = Path.Combine(baseDir, "app", AppScriptName);

        if (!File.Exists(pythonwPath))
        {
            ShowError("Missing runtime\\pythonw.exe. Please keep the whole portable folder together.");
            return 1;
        }

        if (!File.Exists(scriptPath))
        {
            ShowError("Missing app\\" + AppScriptName + ". Please keep the whole portable folder together.");
            return 1;
        }

        try
        {
            ProcessStartInfo startInfo = new ProcessStartInfo();
            startInfo.FileName = pythonwPath;
            startInfo.Arguments = Quote(scriptPath) + BuildForwardedArguments(args);
            startInfo.WorkingDirectory = Path.GetDirectoryName(scriptPath);
            startInfo.UseShellExecute = false;
            startInfo.CreateNoWindow = true;
            startInfo.WindowStyle = ProcessWindowStyle.Hidden;
            Process.Start(startInfo);
            return 0;
        }
        catch (Exception ex)
        {
            ShowError("Failed to start EyeCare 20-20-20.\r\n\r\n" + ex.Message);
            return 1;
        }
    }

    private static string BuildForwardedArguments(string[] args)
    {
        if (args == null || args.Length == 0)
        {
            return string.Empty;
        }

        StringBuilder builder = new StringBuilder();
        foreach (string arg in args)
        {
            builder.Append(' ');
            builder.Append(Quote(arg));
        }
        return builder.ToString();
    }

    private static string Quote(string value)
    {
        if (value == null)
        {
            return "\"\"";
        }

        return "\"" + value.Replace("\"", "\\\"") + "\"";
    }

    private static void ShowError(string message)
    {
        MessageBox.Show(message, "EyeCare 20-20-20", MessageBoxButtons.OK, MessageBoxIcon.Error);
    }
}
