function ScriptBlock() {
    const [copied, setCopied] = React.useState(false);
    const scriptText = `loadstring(game:HttpGet("https://raw.githubusercontent.com/imcomingforyou6959-gif/rawr.xyz/refs/heads/main/.exe"))()`;

    const handleCopy = () => {
        navigator.clipboard.writeText(scriptText);
        setCopied(true);
        setTimeout(() => setCopied(false), 2000);
    };

    return (
        <div className="w-full max-w-lg bg-black/40 border border-white/10 rounded-lg overflow-hidden backdrop-blur-sm shadow-2xl">
            {/* Terminal Header */}
            <div className="flex items-center justify-between px-4 py-2 bg-white/5 border-b border-white/10">
                <div className="flex gap-1.5">
                    <div className="w-2.5 h-2.5 rounded-full bg-red-500/50"></div>
                    <div className="w-2.5 h-2.5 rounded-full bg-yellow-500/50"></div>
                    <div className="w-2.5 h-2.5 rounded-full bg-green-500/50"></div>
                </div>
                <span className="text-[10px] font-mono text-white/30 uppercase tracking-widest">Executor Script</span>
            </div>

            {/* Script Content */}
            <div className="p-4 relative group">
                <code className="block font-mono text-xs md:text-sm text-primary/90 break-all leading-relaxed">
                    <span className="text-white/40">$</span> {scriptText}
                </code>
                
                {/* Copy Button */}
                <button 
                    onClick={handleCopy}
                    className="mt-4 w-full py-2 rounded font-mono text-xs transition-all duration-300 border border-primary/20 hover:bg-primary/10 text-primary"
                >
                    {copied ? "COPIED TO CLIPBOARD" : "CLICK TO COPY SCRIPT"}
                </button>
            </div>
        </div>
    );
}