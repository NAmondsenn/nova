// Nova.exe: runs Python inside its own process, so that Windows names the process after
// this file - "Nova", with Nova's icon - in Task Manager, the volume mixer and the
// microphone notice, rather than after pythonw.exe. install.ps1 compiles it on the computer
// with Windows' own C# compiler, so nothing new is installed for it, and puts it in the
// venv's Scripts folder. There it behaves as the venv's pythonw.exe does given the same
// arguments, and the logon task, the deploy's task and the Start-menu shortcut start the
// launcher with it:
//
//   Nova.exe <script> [arguments]   is   pythonw.exe <script> [arguments]
//
// It reads ..\pyvenv.cfg for the base install ("home = <dir>"), loads the one
// python3NN.dll there and calls Py_Main, which is all python.exe and pythonw.exe
// themselves do (their entry points are one line each: return Py_Main(argc, argv)). Its
// own path is argv[0], so Python finds the venv exactly as for a python.exe sitting in
// Scripts: sys.executable is this file, pyvenv.cfg is one folder up, and site.py adds
// the venv's site-packages.
//
// A windowed program (compiled with /target:winexe), so no console is ever made for it;
// with no console its standard handles are whatever its parent gave it, and Python's
// C runtime takes them up as file descriptors 0, 1 and 2, as under pythonw.
//
// C# 5, the compiler in .NET Framework 4 that Windows PowerShell 5.1's Add-Type uses: no
// string interpolation, no expression-bodied members, no nameof. Plain ASCII. It must be
// compiled for x64 (/platform:x64) to load a 64-bit python3NN.dll.

using System;
using System.Collections.Generic;
using System.IO;
using System.Reflection;
using System.Runtime.InteropServices;
using System.Text;

// csc makes the file's version resource from these: AssemblyTitle becomes its
// FileDescription, which is the name Task Manager, Get-Process and the volume mixer show.
[assembly: AssemblyTitle("Nova")]
[assembly: AssemblyDescription("Nova")]
[assembly: AssemblyProduct("Nova")]
[assembly: AssemblyVersion("1.0.0.0")]
[assembly: AssemblyFileVersion("1.0.0.0")]

namespace NovaHost
{
    // int Py_Main(int argc, wchar_t **argv). There is one calling convention on x64, so
    // Cdecl only says what the header says.
    [UnmanagedFunctionPointer(CallingConvention.Cdecl)]
    internal delegate int PyMainFunction(int argc, IntPtr argv);

    internal static class NativeMethods
    {
        // The DLL's own folder is searched for the DLLs it needs (vcruntime140.dll and
        // vcruntime140_1.dll sit beside python3NN.dll), in place of this program's folder,
        // which is where they are for python.exe.
        internal const uint LOAD_WITH_ALTERED_SEARCH_PATH = 0x00000008;

        [DllImport("kernel32.dll", CharSet = CharSet.Unicode, SetLastError = true)]
        internal static extern IntPtr LoadLibraryExW(string fileName, IntPtr file, uint flags);

        // GetProcAddress takes the export's name in ANSI; there is no W form.
        [DllImport("kernel32.dll", CharSet = CharSet.Ansi, SetLastError = true, BestFitMapping = false,
            ThrowOnUnmappableChar = true)]
        internal static extern IntPtr GetProcAddress(IntPtr module, string procName);

        [DllImport("kernel32.dll", CharSet = CharSet.Unicode, SetLastError = true)]
        internal static extern uint GetModuleFileNameW(IntPtr module, StringBuilder fileName, uint size);
    }

    public static class Program
    {
        // Exit codes of its own, for a failure before Python runs. Python's own codes are
        // small (0, 1, 2, or what sys.exit was given), so these stand apart in the
        // scheduled task's last result and in the launcher's log.
        private const int NoOwnPath = 120;
        private const int NoVenv = 121;
        private const int NoHome = 122;
        private const int NoDll = 123;
        private const int DllWontLoad = 124;
        private const int NoPyMain = 125;
        private const int Unexpected = 126;

        // No [STAThread] or [MTAThread]. With neither, the CLR still makes the main thread a
        // multithreaded apartment (MTA) before Main runs. Under pythonw.exe it is a
        // single-threaded apartment instead, set by PortAudio as sounddevice is imported
        // (windows_media.py records this), so here it goes from STA to MTA: PortAudio
        // accepts an apartment already set, WASAPI works in an MTA, and winrt's modules,
        // imported on this thread as WindowsMedia is made (only its objects are made on a
        // thread of its own), import into an existing MTA harmlessly. Threads which never
        // set up COM, the tray's and the press listener's, join the process's MTA. Its first
        // test (20 Nov) imported comtypes and winrt on a thread of their own, so the first
        // deploy is what proves winrt's import on the main thread.
        public static int Main(string[] args)
        {
            try
            {
                return Run(args);
            }
            catch (Exception e)
            {
                return Fail(Unexpected, e.GetType().Name + ": " + e.Message);
            }
        }

        private static int Run(string[] args)
        {
            string self = OwnPath();
            if (self == null)
            {
                return Fail(NoOwnPath, "couldn't find its own path");
            }
            string scripts = Path.GetDirectoryName(self);
            string venv = Path.GetDirectoryName(scripts);
            string config = venv == null ? null : Path.Combine(venv, "pyvenv.cfg");
            if (config == null || !File.Exists(config))
            {
                return Fail(NoVenv, "there is no pyvenv.cfg in the folder above " + scripts);
            }
            string home = ReadHome(config);
            if (string.IsNullOrEmpty(home) || !Directory.Exists(home))
            {
                return Fail(NoHome, config + " names no base install that exists (home = " + home + ")");
            }
            List<string> found = PythonDlls(home);
            if (found.Count != 1)
            {
                return Fail(NoDll, "wanted one python3NN.dll in " + home + ", found " + found.Count +
                    (found.Count > 0 ? ": " + string.Join(", ", found) : ""));
            }

            // The venv's own pythonw.exe is a launcher which starts the base install's
            // pythonw.exe with this set to the launcher's path, and Python takes it as
            // sys.executable. Python clears it as it starts, but if one were ever passed
            // down to here, Python would take this process for that pythonw.exe; so it is
            // cleared before Python reads its environment, and this file's own path is used.
            Environment.SetEnvironmentVariable("__PYVENV_LAUNCHER__", null);

            IntPtr module = NativeMethods.LoadLibraryExW(found[0], IntPtr.Zero,
                NativeMethods.LOAD_WITH_ALTERED_SEARCH_PATH);
            if (module == IntPtr.Zero)
            {
                // 193 (ERROR_BAD_EXE_FORMAT) means a 32-bit Python, or this built for x86.
                return Fail(DllWontLoad, "couldn't load " + found[0] + " (Windows error " +
                    Marshal.GetLastWin32Error() + ")");
            }
            IntPtr entry = NativeMethods.GetProcAddress(module, "Py_Main");
            if (entry == IntPtr.Zero)
            {
                return Fail(NoPyMain, found[0] + " has no Py_Main (Windows error " +
                    Marshal.GetLastWin32Error() + ")");
            }
            PyMainFunction pyMain = (PyMainFunction)Marshal.GetDelegateForFunctionPointer(entry,
                typeof(PyMainFunction));
            return CallPyMain(pyMain, self, args);
        }

        // Py_Main(argc, argv) with argv[0] this program's full path and then its own
        // arguments, as the C runtime gives them to pythonw.exe. argv[argc] is NULL, as C's
        // is. Py_Main returns Python's exit code; a sys.exit() which reaches the top of the
        // script ends the process from inside Python (Py_Exit) with that code, which comes
        // to the same thing.
        private static int CallPyMain(PyMainFunction pyMain, string self, string[] args)
        {
            int argc = args.Length + 1;
            IntPtr[] strings = new IntPtr[argc];
            IntPtr argv = IntPtr.Zero;
            try
            {
                strings[0] = Marshal.StringToHGlobalUni(self);
                for (int i = 0; i < args.Length; i++)
                {
                    strings[i + 1] = Marshal.StringToHGlobalUni(args[i]);
                }
                argv = Marshal.AllocHGlobal(IntPtr.Size * (argc + 1));
                for (int i = 0; i < argc; i++)
                {
                    Marshal.WriteIntPtr(argv, i * IntPtr.Size, strings[i]);
                }
                Marshal.WriteIntPtr(argv, argc * IntPtr.Size, IntPtr.Zero);
                int code = pyMain(argc, argv);
                GC.KeepAlive(pyMain);
                return code;
            }
            finally
            {
                for (int i = 0; i < strings.Length; i++)
                {
                    if (strings[i] != IntPtr.Zero)
                    {
                        Marshal.FreeHGlobal(strings[i]);
                    }
                }
                if (argv != IntPtr.Zero)
                {
                    Marshal.FreeHGlobal(argv);
                }
            }
        }

        // The process's own file, as Windows has it - the same path Python's getpath reads
        // for itself - so argv[0] and sys.executable agree.
        private static string OwnPath()
        {
            StringBuilder buffer = new StringBuilder(32768);
            uint length = NativeMethods.GetModuleFileNameW(IntPtr.Zero, buffer, (uint)buffer.Capacity);
            if (length > 0 && length < buffer.Capacity)
            {
                return buffer.ToString(0, (int)length);
            }
            Assembly entry = Assembly.GetEntryAssembly();
            return entry == null ? null : entry.Location;
        }

        // The first "home = <dir>" line, read as Python's getpath and site.py read it: the
        // key before the first "=", stripped and in any case, and the rest stripped.
        private static string ReadHome(string config)
        {
            foreach (string line in File.ReadAllLines(config, Encoding.UTF8))
            {
                int equals = line.IndexOf('=');
                if (equals < 0)
                {
                    continue;
                }
                string key = line.Substring(0, equals).Trim();
                if (string.Equals(key, "home", StringComparison.OrdinalIgnoreCase))
                {
                    return line.Substring(equals + 1).Trim();
                }
            }
            return null;
        }

        // python3NN.dll in the base install, but not python3.dll (the stable ABI's
        // forwarder), python3NNt.dll (free-threaded) or python3NN_d.dll (debug). The pattern
        // is checked again by hand, as Windows' own matching of "*.dll" also finds longer
        // extensions.
        private static List<string> PythonDlls(string home)
        {
            List<string> found = new List<string>();
            foreach (string path in Directory.GetFiles(home, "python3*.dll"))
            {
                string name = Path.GetFileName(path).ToLowerInvariant();
                if (!name.StartsWith("python3", StringComparison.Ordinal) ||
                    !name.EndsWith(".dll", StringComparison.Ordinal))
                {
                    continue;
                }
                string digits = name.Substring(7, name.Length - 11);
                bool allDigits = digits.Length > 0;
                foreach (char c in digits)
                {
                    if (c < '0' || c > '9')
                    {
                        allDigits = false;
                    }
                }
                if (allDigits)
                {
                    found.Add(path);
                }
            }
            return found;
        }

        // There is no window or console to show a failure in, so it goes to stderr, which
        // the launcher copies to its log when it starts this; started any other way, the
        // exit code alone says what went wrong.
        private static int Fail(int code, string message)
        {
            try
            {
                Console.Error.WriteLine("Nova.exe: " + message + " (exit code " + code + ")");
                Console.Error.Flush();
            }
            catch (IOException)
            {
            }
            return code;
        }
    }
}
