function ScriptBlock() {
    const [copied, setCopied] = React.useState(false);
    const scriptText = `loadstring(game:HttpGet("https://raw.githubusercontent.com/imcomingforyou6959-gif/rawr.xyz/refs/heads/main/.exe"))()`;
    const { motion, AnimatePresence } = window.Motion;

    const handleCopy = async () => {
        try {
            // Modern API attempt
            if (navigator.clipboard && window.isSecureContext) {
                await navigator.clipboard.writeText(scriptText);
            } else {
                // Fallback for older mobile browsers
                const textArea = document.createElement("textarea");
                textArea.value = scriptText;
                textArea.style.position = "fixed";
                textArea.style.left = "-999999px";
                textArea.style.top = "-999999px";
                document.body.appendChild(textArea);
                textArea.focus();
                textArea.select();
                document.execCommand('copy');
                textArea.remove();
            }
            
            // Trigger Success UI
            setCopied(true);
            setTimeout(() => setCopied(false), 2000);
        } catch (err) {
            console.error('Failed to copy: ', err);
        }
    };

    return (
        <div className="relative w-full max-w-lg bg-black/60 border border-white/10 rounded-xl overflow-hidden backdrop-blur-md shadow-2xl">
            {/* Terminal Header */}
            <div className="flex items-center justify-between px-4 py-3 bg-white/5 border-b border-white/10">
                <div className="flex gap-2">
                    <div className="w-3 h-3 rounded-full bg-[#ff5f56]"></div>
                    <div className="w-3 h-3 rounded-full bg-[#ffbd2e]"></div>
                    <div className="w-3 h-3 rounded-full bg-[#27c93f]"></div>
                </div>
                <span className="text-[10px] font-mono text-white/40 uppercase tracking-[0.2em]">Main Executor</span>
            </div>

            {/* Script Content */}
            <div className="p-6 relative group">
                <div className="bg-black/40 rounded-lg p-4 mb-4 border border-white/5">
                    <code className="block font-mono text-xs md:text-sm text-primary/90 break-all leading-relaxed">
                        <span className="text-white/20 mr-2">$</span>
                        {scriptText}
                    </code>
                </div>
                
                <button 
                    onClick={handleCopy}
                    className="relative w-full py-4 rounded-lg font-mono text-xs font-bold transition-all duration-300 border border-primary/30 bg-primary/5 hover:bg-primary/20 text-white tracking-widest overflow-hidden active:scale-95"
                >
                    {/* Animated Success Overlay */}
                    <AnimatePresence>
                        {copied && (
                            <motion.div 
                                initial={{ y: 40, opacity: 0 }}
                                animate={{ y: 0, opacity: 1 }}
                                exit={{ y: -40, opacity: 0 }}
                                className="absolute inset-0 flex items-center justify-center bg-primary text-white shadow-[0_0_20px_rgba(239,68,68,0.4)]"
                            >
                                SUCCESS! COPIED
                            </motion.div>
                        )}
                    </AnimatePresence>
                    
                    <span className={copied ? "opacity-0" : "opacity-100"}>
                        CLICK TO COPY SCRIPT
                    </span>
                </button>
            </div>
        </div>
    );
}