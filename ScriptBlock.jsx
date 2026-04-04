function ScriptBlock() {
    const [copied, setCopied] = React.useState(false);
    // State to hold our live data
    const [config, setConfig] = React.useState({
        version: "LOADING...",
        status: "CHECKING...",
        statusColor: "gray"
    });

    const scriptText = `loadstring(game:HttpGet("https://raw.githubusercontent.com/imcomingforyou6959-gif/rawr.xyz/refs/heads/main/.exe"))()`;
    
    // Access motion and AnimatePresence from the global window object for CDN usage
    const { motion, AnimatePresence } = window.Motion;

    // This runs as soon as the page loads
    React.useEffect(() => {
        // sup
        const jsonUrl = "https://raw.githubusercontent.com/imcomingforyou6959-gif/rawr.xyl/main/status.json";

        fetch(jsonUrl)
            .then(response => {
                if (!response.ok) throw new Error('Network response was not ok');
                return response.json();
            })
            .then(data => {
                setConfig(data);
            })
            .catch(err => {
                console.error("Failed to load status:", err);
                // Fallback state if GitHub is down or file is missing
                setConfig({ version: "v1.7.2", status: "ONLINE", statusColor: "green" });
            });
    }, []);

    const handleCopy = async () => {
        try {
            if (navigator.clipboard && window.isSecureContext) {
                await navigator.clipboard.writeText(scriptText);
            } else {
                // Fallback for non-https or older browsers
                const textArea = document.createElement("textarea");
                textArea.value = scriptText;
                textArea.style.position = "fixed";
                textArea.style.left = "-999999px";
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

    // Helper logic for colors
    const dotColorClass = config.statusColor === "green" ? "bg-green-500" : 
                         config.statusColor === "red" ? "bg-red-500" : "bg-yellow-500";
    
    const textColorClass = config.statusColor === "green" ? "text-green-500/80" : 
                          config.statusColor === "red" ? "text-red-500/80" : "text-yellow-500/80";

    return (
        <div className="relative w-full max-w-lg bg-black/60 border border-white/10 rounded-xl overflow-hidden backdrop-blur-md shadow-2xl mt-8">
            {/* Terminal Header */}
            <div className="flex items-center justify-between px-4 py-3 bg-white/5 border-b border-white/10">
                <div className="flex items-center gap-4">
                    <div className="flex gap-1.5">
                        <div className="w-2.5 h-2.5 rounded-full bg-[#ff5f56]"></div>
                        <div className="w-2.5 h-2.5 rounded-full bg-[#ffbd2e]"></div>
                        <div className="w-2.5 h-2.5 rounded-full bg-[#27c93f]"></div>
                    </div>
                    <span className="text-[10px] font-mono text-white/30 bg-white/5 px-2 py-0.5 rounded border border-white/5 tracking-wider uppercase">
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

            {/* Terminal Content */}
            <div className="p-6 relative group">
                <div className="bg-black/40 rounded-lg p-4 mb-4 border border-white/5 group-hover:border-primary/20 transition-colors">
                    <code className="block font-mono text-[10px] md:text-xs text-primary/90 break-all leading-relaxed">
                        <span className="text-white/20 mr-2">$</span>
                        {scriptText}
                    </code>
                </div>
                
                <button 
                    onClick={handleCopy}
                    className="relative w-full py-4 rounded-lg font-mono text-xs font-bold transition-all duration-300 border border-primary/30 bg-primary/5 hover:bg-primary/20 text-white tracking-widest overflow-hidden active:scale-95"
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
                                className="block"
                            >
                                CLICK TO COPY SCRIPT
                            </motion.span>
                        )}
                    </AnimatePresence>
                </button>
            </div>
        </div>
    );
}