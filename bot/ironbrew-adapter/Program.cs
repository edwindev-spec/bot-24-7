using System.Text;
using IronbrewDeobfuscator;

if (args.Length == 1 && args[0] == "--version")
{
    Console.WriteLine("IronbrewDeobfuscator adapter 1.0");
    return 0;
}

if (args.Length != 2)
{
    Console.Error.WriteLine("Uso: IronbrewAdapter <entrada.lua> <salida.luac>");
    return 2;
}

try
{
    var source = File.ReadAllText(args[0], Encoding.UTF8);
    var bytecode = Deobfuscation.HandleIb2Deobfuscation(source, enableMaxCflowDeobfuscation: false, isDebug: false).ToArray();
    if (bytecode.Length == 0)
    {
        Console.Error.WriteLine("Ironbrew no produjo bytecode.");
        return 1;
    }

    File.WriteAllBytes(args[1], bytecode);
    return 0;
}
catch (Exception error)
{
    Console.Error.WriteLine($"Ironbrew no pudo procesar el archivo: {error.Message}");
    return 1;
}
