function RainBackground() {
    const canvasRef = React.useRef(null);
    const mouseRef = React.useRef({ x: -1000, y: -1000 }); // Start mouse off-screen

    React.useEffect(() => {
        const canvas = canvasRef.current;
        const ctx = canvas.getContext('2d');
        let animationId;

        const resize = () => {
            canvas.width = window.innerWidth;
            canvas.height = window.innerHeight;
        };

        // Track Mouse Position
        const handleMouseMove = (e) => {
            mouseRef.current = { x: e.clientX, y: e.clientY };
        };

        window.addEventListener('resize', resize);
        window.addEventListener('mousemove', handleMouseMove);
        resize();

        const drops = [];
        const dropCount = 180;

        for (let i = 0; i < dropCount; i++) {
            drops.push({
                x: Math.random() * canvas.width,
                y: Math.random() * canvas.height,
                length: Math.random() * 18 + 8,
                speed: Math.random() * 4 + 3,
                opacity: Math.random() * 0.15 + 0.03,
                width: Math.random() * 1.2 + 0.3,
                offsetX: 0 // Used for the interactive "push"
            });
        }

        const animate = () => {
            ctx.clearRect(0, 0, canvas.width, canvas.height);

            drops.forEach((drop) => {
                // INTERACTIVE LOGIC: Calculate distance from mouse
                const dx = mouseRef.current.x - drop.x;
                const dy = mouseRef.current.y - drop.y;
                const distance = Math.sqrt(dx * dx + dy * dy);
                const forceRange = 120; // How close the mouse needs to be

                if (distance < forceRange) {
                    // Push the rain away from the mouse horizontally
                    const force = (forceRange - distance) / forceRange;
                    drop.offsetX = -dx * force * 0.2; 
                } else {
                    // Smoothly return to normal slant
                    drop.offsetX *= 0.95;
                }

                ctx.beginPath();
                // We add the offsetX to the x-coordinates
                ctx.moveTo(drop.x + drop.offsetX, drop.y);
                ctx.lineTo(drop.x + 2 + drop.offsetX, drop.y + drop.length);
                
                ctx.strokeStyle = 'rgba(255, 100, 100, ${drop.opacity})';
                ctx.lineWidth = drop.width;
                ctx.lineCap = 'round';
                ctx.stroke();

                drop.y += drop.speed;

                if (drop.y > canvas.height) {
                    drop.y = -drop.length;
                    drop.x = Math.random() * canvas.width;
                    drop.offsetX = 0; // Reset push on new drop
                }
            });

            animationId = requestAnimationFrame(animate);
        };

        animate();

        return () => {
            cancelAnimationFrame(animationId);
            window.removeEventListener('resize', resize);
            window.removeEventListener('mousemove', handleMouseMove);
        };
    }, []);

    return (
        <canvas
            ref={canvasRef}
            className="fixed inset-0 pointer-events-none z-0"
        />
    );
}