function RainBackground() {
    const canvasRef = React.useRef(null);

    React.useEffect(() => {
        const canvas = canvasRef.current;
        const ctx = canvas.getContext('2d');
        let animationId;

        const resize = () => {
            canvas.width = window.innerWidth;
            canvas.height = window.innerHeight;
        };
        resize();
        window.addEventListener('resize', resize);

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
            });
        }

        const animate = () => {
            ctx.clearRect(0, 0, canvas.width, canvas.height);

            drops.forEach((drop) => {
                ctx.beginPath();
                ctx.moveTo(drop.x, drop.y);
                // Added a tiny bit of horizontal slant (2) for a more dynamic feel
                ctx.lineTo(drop.x + 2, drop.y + drop.length);
                ctx.strokeStyle = `rgba(140, 160, 255, ${drop.opacity})`;
                ctx.lineWidth = drop.width;
                ctx.lineCap = 'round';
                ctx.stroke();

                drop.y += drop.speed;

                if (drop.y > canvas.height) {
                    drop.y = -drop.length;
                    drop.x = Math.random() * canvas.width;
                }
            });

            animationId = requestAnimationFrame(animate);
        };

        animate();

        return () => {
            cancelAnimationFrame(animationId);
            window.removeEventListener('resize', resize);
        };
    }, []);

    return (
        <canvas
            ref={canvasRef}
            className="fixed inset-0 pointer-events-none z-0"
        />
    );
}