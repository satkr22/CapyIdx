import asyncio

def f_write():
    i = 0
    with open("rex.txt", "w+") as f:
        while True:
            f.write("#")
            i += 1
            if i == 100000000:
                break

async def A():
    print("A started")
    await asyncio.sleep(1)
    print("A continues..")
    
async def B():
    print("B started")
    await asyncio.to_thread(f_write)
    print("B over")
    
async def main():
    t1 = asyncio.create_task(A())
    t2 = asyncio.create_task(B())
    
    # await t1
    # await t2
    # instead of these two above, do this
    await asyncio.gather(t1, t2) # this is better way of saying wait untill these tasks are finished

if __name__ == "__main__":
    asyncio.run(main())