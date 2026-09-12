# import asyncio
# import os

# from utils.disk_operations import DiskOperations
# from walker.walk_dir import walk_dir, walk_dir_async

# async def main():
#     repo = os.path.abspath("/home/usatkr/u_ml/projects/AI_Copilot")
#     disk = DiskOperations([repo])

#     print("=== Streaming ===")
#     async for f in walk_dir_async(disk._roots[0], disk):
#         print(f)

#     print("\n=== List ===")
#     files = await walk_dir(disk._roots[0], disk)
#     print(*files, sep="\n")

# asyncio.run(main())




import asyncio
import os

from walker.walk_dir import walk_dir, walk_dir_async, WalkerOptions
from utils.disk_operations import DiskOperations

repo = os.path.abspath("/home/usatkr/u_ml/projects/AI_Copilot")

async def test_walk_dir(root, disk):
    files = await walk_dir(root, disk)

    print("Returned order:")
    for f in files:
        print(f)

async def test_walk_dir_async(root, disk):
    async for file in walk_dir_async(root, disk):
        print("Received:", file)
        
        
async def test_dirs_only(root, disk):
    dirs = await walk_dir(
        root,
        disk,
        WalkerOptions(include="dirs")
    )

    for d in dirs:
        print(d)
        
import asyncio

async def test_cache(root, disk):
    print("First walk")
    await walk_dir(root, disk)

    print("\nSecond walk (immediately)")
    await walk_dir(root, disk)

    print("\nWaiting 35 seconds...")
    await asyncio.sleep(35)

    print("\nThird walk")
    await walk_dir(root, disk)

async def main():
    disk = DiskOperations([repo])
    root = (await disk.get_workspace_dirs())[0]

    # Uncomment one test at a time
    await test_walk_dir(root, disk)
    # await test_walk_dir_async(root, disk)
    # await test_dirs_only(root, disk)
    # await test_cache(root, disk)

asyncio.run(main())

