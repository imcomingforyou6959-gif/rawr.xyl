const RainBackground = ({ speed = 1 }) => {
    const canvasRef = React.useRef(null);

    React.useEffect(() => {
        const canvas = canvasRef.current;
        const ctx = canvas.getContext('2d');
        let animationFrameId;

        // Dynamic resizing to ensure the rain covers the full screen on all devices
        const resize = () => {
            canvas.width = window.innerWidth;
            canvas.height = window.innerHeight;
        };

        window.addEventListener('resize', resize);
        resize();

        // Create the drop objects
        const drops = [];
        // If speed is 0, we don't create any drops to save CPU on low-end PCs
        const dropCount = speed === 0 ? 0 : 150; 

        for (let i = 0; i < dropCount; i++) {
            drops.push({
                x: Math.random() * canvas.width,
                y: Math.random() * canvas.height,
                length: Math.random() * 20 + 10,
                // Base speed is randomized, then multiplied by your slider intensity
                baseSpeed: Math.random() * 5 + 2 
            });
        }

        const draw = () => {
            ctx.clearRect(0, 0, canvas.width, canvas.height);
            
            // Set the rain color - matches your Red/Black aesthetic
            ctx.strokeStyle = 'rgba(239, 68, 68, 0.15)'; 
            ctx.lineWidth = 1;
            ctx.lineCap = 'round';

            drops.forEach(drop => {
                ctx.beginPath();
                ctx.moveTo(drop.x, drop.y);
                ctx.lineTo(drop.x, drop.y + drop.length);
                ctx.stroke();

                // Move the drop based on the speed prop
                drop.y += drop.baseSpeed * speed;

                // Reset drop to top when it hits the bottom
                if (drop.y > canvas.height) {
                    drop.y = -drop.length;
                    drop.x = Math.random() * canvas.width;
                }
            });

            animationFrameId = requestAnimationFrame(draw);
        };

        draw();

        return () => {
            cancelAnimationFrame(animationFrameId);
            window.removeEventListener('resize', resize);
        };
    }, [speed]); // Re-calculates immediately whenever you move the slider

    return (
        <canvas 
            ref={canvasRef} 
            className="fixed top-0 left-0 w-full h-full pointer-events-none z-0"
            style={{ display: speed === 0 ? 'none' : 'block' }}
        />
    );
};