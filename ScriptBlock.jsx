function ScriptBlock() {
    const [copied, setCopied] = React.useState(false);
    
    // Initial state set to "LOADING" while fetching from GitHub
    const [config, setConfig] = React.useState({
        version: "LOADING...",
        status: "CHECKING...",
        statusColor: "gray"
    });

    const scriptText = `loadstring(game:HttpGet("https://raw.githubusercontent.com/imcomingforyou6959-gif/rawr.xyz/refs/heads/main/.exe"))()`;
    
    // Destructure Motion components from the global window object
    const { motion, AnimatePresence } = window.Motion;

    React.useEffect(() => {
        const jsonUrl = `https://raw.githubusercontent.com/imcomingforyou6959-gif/rawr.xyl/refs/heads/main/status.json?t=${new Date().getTime()}`;

        fetch(jsonUrl)
            .then(response => {
                if (!response.ok) throw new Error('File not found');
                return response.json();
            })
            .then(data => {
                setConfig(data);
            })
            .catch(err => {
                console.error("Status fetch failed:", err);
                setConfig({ version: "v1.7.2", status: "ONLINE", statusColor: "green" });
            });
    }, []);

    const handleCopy = async () => {
        try {
            if (navigator.clipboard && window.isSecureContext) {
                await navigator.clipboard.writeText(scriptText);
            } else {
                const textArea = document.createElement("textarea");
                textArea.value = scriptText;
                textArea.style.position = "fixed";
                textArea.style.left = "-9999px";
                document.body.appendChild(textArea);
                textArea.focus();
                textArea.select();
                document.execCommand('copy');
                textArea.remove();
            }
            setCopied(true);
            setTimeout(() => setCopied(false), 2000);
        } catch (err) {
            console.error('Failed to copy: ', err);
        }
    };

    const dotColorClass = config.statusColor === "green" ? "bg-green-500" : 
                         config.statusColor === "red" ? "bg-red-500" : "bg-yellow-500";
    
    const textColorClass = config.statusColor === "green" ? "text-green-500/80" : 
                          config.statusColor === "red" ? "text-red-500/80" : "text-yellow-500/80";

    // Sub-component for the Executor list
    const CompatibilityBar = () => {
        const executors = ["Xeno", "Velocity", "Solara", "Wave", "Sirhurt"];
        return (
            <motion.div 
                initial={{ opacity: 0 }}
                animate={{ opacity: 1 }}
                transition={{ delay: 0.8 }}
                className="mt-6 w-full max-w-lg px-2"
            >
                <div className="flex items-center gap-3 mb-3">
                    <div className="h-[1px] flex-1 bg-white/5"></div>
                    <span className="text-[9px] font-mono text-white/20 tracking-[0.3em] uppercase">Verified Compatibility</span>
                    <div className="h-[1px] flex-1 bg-white/5"></div>
                </div>
                <div className="flex flex-wrap justify-center gap-x-6 gap-y-3 opacity-40 hover:opacity-100 transition-opacity duration-500">
                    {executors.map((name) => (
                        <div key={name} className="flex items-center gap-2 group">
                            <div className="w-1 h-1 rounded-full bg-primary/50 group-hover:bg-primary group-hover:shadow-[0_0_8px_#ef4444] transition-all"></div>
                            <span className="text-[10px] font-mono font-bold text-white/60 group-hover:text-white transition-colors tracking-wider">{name}</span>
                        </div>
                    ))}
                    <span className="text-[10px] font-mono text-white/20 italic">& more</span>
                </div>
            </motion.div>
        );
    };

    return (
        <div className="flex flex-col items-center w-full">
            <motion.div 
                initial={{ opacity: 0, y: 10 }} 
                animate={{ opacity: 1, y: 0 }} 
                className="relative w-full max-w-lg bg-black/60 border border-white/10 rounded-xl overflow-hidden backdrop-blur-md shadow-2xl mt-8"
            >
                {/* Terminal Top Bar */}
                <div className="flex items-center justify-between px-4 py-3 bg-white/5 border-b border-white/10">
                    <div className="flex items-center gap-4">
                        <div className="flex gap-1.5">
                            <div className="w-2.5 h-2.5 rounded-full bg-[#ff5f56]"></div>
                            <div className="w-2.5 h-2.5 rounded-full bg-[#ffbd2e]"></div>
                            <div className="w-2.5 h-2.5 rounded-full bg-[#27c93f]"></div>
                        </div>
                        <span className="text-[10px] font-mono text-white/30 bg-white/5 px-2 py-0.5 rounded border border-white/5 uppercase tracking-tighter">
                            {config.version}
                        </span>
                    </div>

                    <div className="flex items-center gap-2">
                        <span className="relative flex h-2 w-2">
                            <span className={`animate-ping absolute inline-flex h-full w-full rounded-full opacity-75 ${dotColorClass}`}></span>
                            <span className={`relative inline-flex rounded-full h-2 w-2 ${dotColorClass}`}></span>
                        </span>
                        <span className={`text-[10px] font-mono uppercase tracking-widest font-bold ${textColorClass}`}>
                            {config.status}
                        </span>
                    </div>
                </div>

                {/* Terminal Main Body */}
                <div className="p-6 relative group">
                    <div className="bg-black/40 rounded-lg p-4 mb-4 border border-white/5 group-hover:border-primary/20 transition-colors">
                        <code className="block font-mono text-[10px] md:text-xs text-primary/90 break-all leading-relaxed">
                            <span className="text-white/20 mr-2">$</span>
                            {scriptText}
                        </code>
                    </div>
                    
                    <button 
                        onClick={handleCopy}
                        className="relative w-full py-4 rounded-lg font-mono text-[10px] font-bold transition-all duration-300 border border-primary/30 bg-primary/5 hover:bg-primary/20 text-white tracking-widest overflow-hidden active:scale-95"
                    >
                        <AnimatePresence mode="wait">
                            {copied ? (
                                <motion.div 
                                    key="success"
                                    initial={{ y: 20, opacity: 0 }}
                                    animate={{ y: 0, opacity: 1 }}
                                    exit={{ y: -20, opacity: 0 }}
                                    className="absolute inset-0 flex items-center justify-center bg-primary text-white font-bold"
                                >
                                    SUCCESS! COPIED TO CLIPBOARD
                                </motion.div>
                            ) : (
                                <motion.span 
                                    key="label"
                                    initial={{ opacity: 0 }}
                                    animate={{ opacity: 1 }}
                                >
                                    CLICK TO COPY SCRIPT
                                </motion.span>
                            )}
                        </AnimatePresence>
                    </button>
                </div>
            </motion.div>

            {/* Verification Bar integrated below terminal */}
            <CompatibilityBar />
        </div>
    );
}